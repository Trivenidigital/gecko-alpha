"""Solana DEX execution lane — the permanent runner.

The operator-invoked CLI that executes ONE supervised SOL->USDC swap on
mainnet behind a manual approval boundary. It composes PR-S1's venue clients
(``scout.live.solana``) with the existing kill switch and ``live_trades``
ledger; it is NOT wired into the signal-driven live engine and nothing in the
pipeline imports it.

Usage::

    python -m scout.live.solana_lane place --sol 0.05 \\
        [--simulate-only --yes-i-am-rehearsing]
    python -m scout.live.solana_lane status
    python -m scout.live.solana_lane resolve --decision-id <uuid>

    python -m scout.live.solana_lane reverse --live-trade-id 5 --usdc 6.899618 \\
        [--close-usdc-ata] [--simulate-only --yes-i-am-rehearsing]
    python -m scout.live.solana_lane reverse-status
    python -m scout.live.solana_lane reverse-resolve --decision-id <uuid>

The reverse command — closing a position, not opening one
---------------------------------------------------------
``reverse`` runs the SAME pipeline in the other direction: Jupiter quote and
build -> deterministic validation -> independent simulation -> typed
authorization -> funded key loaded only after that -> expected signature
persisted -> exactly-once Jito submission -> finality and balance
reconciliation. It is a new command over proven internals, not a second lane;
the gates, the limits engine, the state machine, the signer, the resolver pool
and the evidence format are the ones ``place`` already uses.

Four things genuinely differ, and each is a decision rather than an oversight:

**It is named for a row.** The operator passes ``--live-trade-id``. A row at
another venue is refused (Kraken positions are closed through the Kraken lane),
and so is a row that is not ``open``. There is no "find the position" step: a
command that guessed which position to sell would be a command that can sell
the wrong one.

**The authorization binds a DIGEST, not the message hash alone.** The message
carries the amount, route, slippage, fees and blockhash — but not WHICH LEDGER
ROW is being closed, nor whether the USDC account is to be closed with it. Both
are in the digest, and the message hash is one of its inputs, so the binding is
strictly stronger than the forward lane's. See "Reverse-swap authorization
binding" below.

**The row state and both balances are re-read immediately before SIGNING**, not
merely before the approval screen. The prompt is unbounded operator time, the
lane lock excludes only another lane process, and a wallet can be moved from a
phone. Any difference voids the authorization and the run stops; it never
rebuilds.

**The row closes only on finality plus exact agreement.** A finalized
transaction whose USDC debit is exactly the authorized amount and whose SOL
credit falls inside the authorized band closes the row. Partial, ambiguous,
timed-out, unfinalized or mismatched outcomes leave it OPEN — the position
still exists, and a ledger that said otherwise would be the one thing every
later decision is made against.

Closing the wallet's USDC account and recovering its ~2,039,280 lamports of
rent is a SEPARATE option, off by default, requiring both
``SOLANA_REVERSE_CLOSE_USDC_ATA`` and ``--close-usdc-ata``. With it off the
inspector REFUSES a build that closes that account, so "it was not bundled in
quietly" is enforced rather than asserted. With it on, the closure and the
exact rent get their own line on the approval screen.

Place flow, in mechanical order — every step is printed, logged and appended
to the evidence file before the next one starts:

1.  envelope gate (SOLANA_MODE, keypair path, mainnet hosts)
2.  kill-switch check
3.  keypair custody — load at call time, derive the signer pubkey
4.  resolver-pool validation (every endpoint proves it is on mainnet-beta and
    caught up), then startup reconciliation — a restart must not forget a
    signature
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
**Authorization gates SIGNING, not submission.** The funded lane key does
not sign until the operator has typed the authorization — including in
rehearsal. A validly signed transaction is an irreversible-capability
artifact: anyone who obtains those bytes can broadcast them, and the wallet
cannot take that back. Holding them only in memory bounds the EXPOSURE, but
it does not reduce the CAPABILITY that was created, and it is the capability
the authorization exists to gate. So the order is build -> inspect ->
simulate (unsigned) -> approve -> load key -> sign -> persist -> submit.

The authorization therefore binds to the TRANSACTION MESSAGE HASH — the
sha256 of the exact bytes the key will sign, which ``tx_inspector`` computes
from the unsigned transaction. It keeps the property the signature had: any
change to the quote, route, amount, fees, slippage, blockhash or instructions
changes the message, changes the hash, and invalidates the authorization. The
operator types the first 8 characters of that hash.

The funded key is not merely unused before authorization — it is not READ.
The signer's public key comes from ``SOLANA_PILOT_SIGNER_PUBKEY`` so that
Jupiter can build for it, the inspector can check the fee payer against it and
the balance gate can read it, all without opening the key file. Custody is
verified by ``stat`` alone (mode and owner), never by loading. A refusal at
any gate, and every ``--simulate-only`` run, leaves the key file untouched.

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

**The resolver reads from a POOL; one resolution reads from one node.**
``SOLANA_RESOLVER_RPC_URLS`` is an ordered list and ``SOLANA_RESOLVER_RPC_URL``
is read as a one-element pool, which is what is deployed. Every endpoint must
prove via ``getGenesisHash`` that it is on mainnet-beta before it is used, and
selection is deterministic in the signature so a resolution's two sweeps never
straddle two nodes. A second endpoint buys read failover (an unreachable
resolver blocks the lane) and corroboration of the one verdict that clears it.
Adding one is configuration; the code path is the same one a single endpoint
already runs. See ``scout.live.solana.resolver_pool``.

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

**``paper_trade_id`` is satisfied by a lane ANCHOR row.**
``live_trades.paper_trade_id`` is ``NOT NULL REFERENCES paper_trades(id)`` with
``PRAGMA foreign_keys=ON``, and there is no signal behind an operator-invoked
swap. One sentinel row — token_id 'solana-lane', signal_type 'solana_lane',
status 'solana_lane_anchor', opened_at epoch — is created once and referenced
by every lane swap. The three field values are each load-bearing against a
specific reader: the status is not 'open' so no open-position loop scans it,
the epoch ``opened_at`` keeps it out of the daily digest's
``date(opened_at) = ?`` count and the analytics windows' ``opened_at >=
cutoff``, and ``closed_at`` stays NULL so the closed-trade digest never sees
it. No schema migration.

**A landed swap leaves the row 'open'.** 'open' here means "this lane holds
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
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import aiohttp
import structlog
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from scout.config import Settings, load_settings
from scout.db import Database
from scout.db_path import UnsafeDatabase, assert_safe_database
from scout.live.kill_switch import KillSwitch
from scout.live.solana.constants import (
    ATA_RENT_LAMPORTS_FALLBACK,
    JITO_TIP_ACCOUNTS_FALLBACK,
    LAMPORTS_PER_SIGNATURE,
    LAMPORTS_PER_SOL,
    SOL_DECIMALS,
    SOL_MINT,
    SOLANA_MAINNET_GENESIS_HASH,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_ACCOUNT_DATA_LENGTH,
    TOKEN_PROGRAM_ID,
    USDC_DECIMALS,
    USDC_MINT,
)
from scout.live.solana.execution_watchdog import check_stuck_executions
from scout.live.solana.exceptions import (
    SolanaAmbiguousSubmissionError,
    SolanaAPIError,
    SolanaKeypairError,
    SolanaLimitBreached,
)
from scout.live.solana.jito_client import JitoClient
from scout.live.solana.jupiter_client import JupiterClient, SolanaQuote, SwapBuild
from scout.live.solana.limits import (
    DIRECTION_SOL_TO_USDC,
    DIRECTION_USDC_TO_SOL,
    LaneExposure,
    LimitsEngine,
    LimitsReport,
    OperatorExitBinding,
    notional_usd_today,
)
from scout.live.solana.resolver import ResolutionReport
from scout.live.solana.resolver_pool import (
    PoolResolution,
    ResolverPool,
    redact_endpoint,
    resolve_with_pool,
    resolver_urls,
)
from scout.live.solana.rpc_client import SolanaRpcClient
from scout.live.solana.signer import (
    SignedTransaction,
    enforce_keyfile_security,
    load_keypair,
    sign_transaction,
)
from scout.live.solana.tx_inspector import (
    VerificationReport,
    derive_associated_token_address,
    verify_swap_transaction,
)

log = structlog.get_logger(__name__)

# Exit codes. Distinct rather than a blanket 1 because the operator's next
# action differs per class, and a wrapper script should be able to tell them
# apart without parsing stdout.
#
#   0  EXIT_OK        the swap landed and reconciled, or nothing was pending.
#   1  EXIT_REFUSED   a gate said no, the operator did not authorize, or the
#                     transaction definitively did not execute. No position.
#   2  EXIT_BLOCKED   the lane is blocked — an unresolved prior signature, or
#                     another lane process holding the lock. Nothing tried.
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

# The ledger status a reverse swap writes when it closes the position it was
# named for. Same value the Kraken pilot's exit uses and for the same reason:
# the CHECK constraint's other closed_* members each assert a stop reason
# ('closed_tp', 'closed_sl', 'closed_duration') that a supervised operator
# close did not have, and widening the constraint for one lane would be a
# migration in service of a label.
_REVERSE_CLOSED_STATUS = "closed_via_reconciliation"


# ----------------------------------------------------------------------
# Swap direction
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class SwapDirection:
    """One traversal of the ONE approved pair.

    The lane trades SOL/USDC and nothing else; a direction says which way. It
    exists so the gates that genuinely differ — the mints quoted, which leg is
    the dollar-denominated one, which accounts a transaction may create and
    close — are parameters of a single pipeline rather than a second copy of
    it. Everything not named here (kill switch, custody, inspection, limits
    engine, signer, state machine, resolver, evidence) is direction-blind on
    purpose: a reverse swap that could take a different path through any of
    those would be a second lane wearing this one's name.
    """

    name: str
    input_mint: str
    output_mint: str
    input_symbol: str
    output_symbol: str
    input_decimals: int
    output_decimals: int
    label: str

    @property
    def is_reverse(self) -> bool:
        return self.name == DIRECTION_USDC_TO_SOL


DIRECTION_FORWARD = SwapDirection(
    name=DIRECTION_SOL_TO_USDC,
    input_mint=SOL_MINT,
    output_mint=USDC_MINT,
    input_symbol="SOL",
    output_symbol="USDC",
    input_decimals=SOL_DECIMALS,
    output_decimals=USDC_DECIMALS,
    label="SOL -> USDC",
)

DIRECTION_REVERSE = SwapDirection(
    name=DIRECTION_USDC_TO_SOL,
    input_mint=USDC_MINT,
    output_mint=SOL_MINT,
    input_symbol="USDC",
    output_symbol="SOL",
    input_decimals=USDC_DECIMALS,
    output_decimals=SOL_DECIMALS,
    label="USDC -> SOL",
)

# The lane anchor (see module docstring). Values are matched exactly on
# lookup, so they are constants rather than anything derived at runtime.
_ANCHOR_TOKEN_ID = "solana-lane"
_ANCHOR_SIGNAL_TYPE = "solana_lane"
_ANCHOR_STATUS = "solana_lane_anchor"
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

# ----------------------------------------------------------------------
# Durable execution states
# ----------------------------------------------------------------------
# One run walks this vocabulary from left to right. The point of naming them
# is RECOVERY: a process that dies mid-run must be able to say what had
# already happened, and — far more important — what had NOT, so a restart can
# never repeat an irreversible step.
#
# The dangerous boundary is SUBMISSION_ATTEMPTED. Before it, nothing was sent
# and a restart may safely discard the run. At or after it, a transaction may
# exist, and the ONLY permitted recovery is to ask the cluster: no rebuild, no
# resubmission, ever.
STATE_QUOTE_CREATED = "quote_created"
STATE_TRANSACTION_BUILT = "transaction_built"
STATE_SIMULATION_PASSED = "simulation_passed"
STATE_AWAITING_AUTHORIZATION = "awaiting_authorization"
STATE_AUTHORIZED = "authorized"
STATE_SIGNED = "signed"
STATE_SUBMISSION_ATTEMPTED = "submission_attempted"
STATE_LANDED = "landed"
STATE_CONFIRMED = "confirmed"
STATE_FINALIZED = "finalized"
STATE_RECONCILED = "reconciled"
STATE_FAILED = "failed"
STATE_SUBMISSION_UNKNOWN = "submission_unknown"

ALL_STATES = (
    STATE_QUOTE_CREATED,
    STATE_TRANSACTION_BUILT,
    STATE_SIMULATION_PASSED,
    STATE_AWAITING_AUTHORIZATION,
    STATE_AUTHORIZED,
    STATE_SIGNED,
    STATE_SUBMISSION_ATTEMPTED,
    STATE_LANDED,
    STATE_CONFIRMED,
    STATE_FINALIZED,
    STATE_RECONCILED,
    STATE_FAILED,
    STATE_SUBMISSION_UNKNOWN,
)

# States from which nothing was ever sent. A restart finding one of these knows
# — from the state alone, without asking the network — that no transaction
# exists, so the run can be discarded and the lane cleared.
PRE_SUBMISSION_STATES = (
    STATE_QUOTE_CREATED,
    STATE_TRANSACTION_BUILT,
    STATE_SIMULATION_PASSED,
    STATE_AWAITING_AUTHORIZATION,
    STATE_AUTHORIZED,
    STATE_SIGNED,
)

# Terminal: the run is over and the lane is not waiting on it.
TERMINAL_STATES = (STATE_RECONCILED, STATE_FAILED)

# States that BLOCK the lane. A transaction may exist and its fate is either
# unknown or requires disposition, so no new execution may start.
BLOCKING_STATES = (
    STATE_SUBMISSION_ATTEMPTED,
    STATE_LANDED,
    STATE_CONFIRMED,
    STATE_FINALIZED,
    STATE_SUBMISSION_UNKNOWN,
)

# Legal transitions. Declared rather than implied by the code's shape so an
# illegal jump — say awaiting_authorization straight to submission_attempted,
# which would mean submitting something nobody approved — is a loud error at
# the write, not a silent state a later reader has to make sense of.
LEGAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATE_QUOTE_CREATED: (STATE_TRANSACTION_BUILT, STATE_FAILED),
    STATE_TRANSACTION_BUILT: (STATE_SIMULATION_PASSED, STATE_FAILED),
    STATE_SIMULATION_PASSED: (STATE_AWAITING_AUTHORIZATION, STATE_FAILED),
    STATE_AWAITING_AUTHORIZATION: (STATE_AUTHORIZED, STATE_FAILED),
    STATE_AUTHORIZED: (STATE_SIGNED, STATE_FAILED),
    STATE_SIGNED: (STATE_SUBMISSION_ATTEMPTED, STATE_FAILED),
    # Past here a transaction may exist, so `failed` is NOT reachable by
    # simply giving up — the only ways out are what the cluster reports.
    STATE_SUBMISSION_ATTEMPTED: (
        STATE_LANDED,
        STATE_CONFIRMED,
        STATE_FINALIZED,
        STATE_SUBMISSION_UNKNOWN,
        STATE_FAILED,
    ),
    STATE_LANDED: (STATE_CONFIRMED, STATE_FINALIZED, STATE_FAILED),
    STATE_CONFIRMED: (STATE_FINALIZED, STATE_RECONCILED, STATE_FAILED),
    STATE_FINALIZED: (STATE_RECONCILED, STATE_FAILED),
    # An unknown submission resolves only into what the cluster says. It can
    # never walk backwards into signing or submitting again.
    STATE_SUBMISSION_UNKNOWN: (
        STATE_LANDED,
        STATE_CONFIRMED,
        STATE_FINALIZED,
        STATE_FAILED,
    ),
    STATE_RECONCILED: (),
    STATE_FAILED: (),
}


class IllegalStateTransition(RuntimeError):
    """A transition the state machine does not permit.

    Raised rather than logged: an illegal transition means the caller believes
    something happened that could not have, and continuing would persist that
    belief.
    """


def assert_legal_transition(current: str | None, nxt: str) -> None:
    """Guard every state write. ``None`` means the run is starting."""
    if nxt not in ALL_STATES:
        raise IllegalStateTransition(f"{nxt!r} is not an execution state")
    if current is None:
        if nxt != STATE_QUOTE_CREATED:
            raise IllegalStateTransition(
                f"a run must begin at {STATE_QUOTE_CREATED}, not {nxt!r}"
            )
        return
    if current not in ALL_STATES:
        raise IllegalStateTransition(f"{current!r} is not an execution state")
    if nxt not in LEGAL_TRANSITIONS[current]:
        raise IllegalStateTransition(
            f"{current} -> {nxt} is not a legal transition; "
            f"from {current} the lane may only reach "
            f"{LEGAL_TRANSITIONS[current] or '(nothing — terminal)'}"
        )


def recovery_disposition(state: str) -> str:
    """What a restart may do with a run left in ``state``.

    Three answers, and the boundary between the first two is the whole reason
    the states are persisted:

    ``discard``  nothing was ever sent; the run can be abandoned and the lane
                 cleared without asking anyone.
    ``resolve``  a transaction may exist. Ask the cluster. NEVER rebuild and
                 never resubmit — a rebuild is a second signature and both can
                 land.
    ``done``     terminal; nothing to recover.
    """
    if state in TERMINAL_STATES:
        return "done"
    if state in PRE_SUBMISSION_STATES:
        return "discard"
    return "resolve"


# ----------------------------------------------------------------------
# Lifecycle <-> money coherence
# ----------------------------------------------------------------------
# Two axes, two tables, and they must never disagree about whether the lane is
# blocked. `solana_executions.state` is the source of truth for WHERE IN THE
# PIPELINE a transaction is; `live_trades.status` is the source of truth for
# WHAT HAPPENED TO THE MONEY. This maps each state to the live_trades statuses
# that can coherently accompany it — an empty tuple meaning "no money row may
# exist yet", because nothing financial has happened.
COHERENT_STATUSES: dict[str, tuple[str, ...]] = {
    # Before authorization there is no money row at all.
    STATE_QUOTE_CREATED: (),
    STATE_TRANSACTION_BUILT: (),
    STATE_SIMULATION_PASSED: (),
    # The intent row is written just before the prompt, so from here a row
    # exists and is 'open' — or 'rejected' if the run was refused.
    STATE_AWAITING_AUTHORIZATION: ("open", "rejected"),
    STATE_AUTHORIZED: ("open", "rejected"),
    STATE_SIGNED: ("open", "rejected"),
    STATE_SUBMISSION_ATTEMPTED: ("open", "rejected"),
    STATE_LANDED: ("open",),
    STATE_CONFIRMED: ("open",),
    STATE_FINALIZED: ("open",),
    # A reconciled swap leaves a REAL position. 'open' here means "this lane
    # holds USDC", which is why it keeps blocking until the operator disposes
    # of it — the execution being finished and the position being open are
    # different facts, and conflating them is what this mapping prevents.
    STATE_RECONCILED: ("open",),
    # Nothing executed: the row asserts no position.
    STATE_FAILED: ("rejected",),
    # A transaction may exist and nobody knows. The money row must say so.
    STATE_SUBMISSION_UNKNOWN: ("needs_manual_review", "open"),
}


class IncoherentLaneState(RuntimeError):
    """The lifecycle axis and the money axis disagree."""


def assert_coherent(state: str, status: str | None) -> None:
    """Guard against the two axes drifting apart.

    ``status=None`` means no live_trades row exists. That is REQUIRED before
    authorization and forbidden after it — a signed transaction with no money
    row would be unrecoverable, and a money row before the quote would be an
    open position backed by nothing.
    """
    permitted = COHERENT_STATUSES.get(state)
    if permitted is None:
        raise IncoherentLaneState(f"{state!r} is not an execution state")
    if status is None:
        if permitted:
            raise IncoherentLaneState(
                f"state {state} requires a live_trades row (one of {permitted}), "
                "but none exists"
            )
        return
    if not permitted:
        raise IncoherentLaneState(
            f"state {state} must not have a live_trades row yet, but one exists "
            f"with status {status!r} — that row would read as a position backed "
            "by no trade"
        )
    if status not in permitted:
        raise IncoherentLaneState(
            f"state {state} is incoherent with live_trades.status {status!r}; "
            f"expected one of {permitted}"
        )


def lane_is_blocked_by(state: str, status: str | None) -> str | None:
    """Why this execution blocks a new one, or None if it does not.

    Both axes can block, for different reasons, and naming which one did is
    what keeps an operator from chasing the wrong table.
    """
    if state in BLOCKING_STATES:
        return f"execution_state={state}"
    if status == "open":
        return "live_trades.status=open (a position is held)"
    if status == "needs_manual_review":
        return "live_trades.status=needs_manual_review"
    return None


# ----------------------------------------------------------------------
# Operating modes
# ----------------------------------------------------------------------
# The whole spectrum the lane will ever run in. Moving between them is
# CONFIGURATION: the same runner, adapter, signer, state machine, limits and
# reconciliation serve all of them, and only the authorization policy differs
# between supervised and autonomous.
MODE_DISABLED = "DISABLED"
MODE_SIMULATION_ONLY = "SIMULATION_ONLY"
MODE_SUPERVISED_LIVE = "SUPERVISED_LIVE"
MODE_BOUNDED_AUTONOMOUS = "BOUNDED_AUTONOMOUS"
MODE_EMERGENCY_STOPPED = "EMERGENCY_STOPPED"

ALL_MODES = (
    MODE_DISABLED,
    MODE_SIMULATION_ONLY,
    MODE_SUPERVISED_LIVE,
    MODE_BOUNDED_AUTONOMOUS,
    MODE_EMERGENCY_STOPPED,
)

# Modes in which a transaction may actually be signed and broadcast. Named as
# a set rather than tested inline so a future mode cannot become executing by
# accident — adding one to this tuple is a deliberate, reviewable act.
EXECUTING_MODES = (MODE_SUPERVISED_LIVE, MODE_BOUNDED_AUTONOMOUS)

# Modes that refuse before anything is built. EMERGENCY_STOPPED is here rather
# than treated as "disabled with a different name": it is the kill switch's
# configuration-side twin and is checked in the same places, so an operator who
# sets it gets the same refusal wherever the kill switch would have produced
# one.
REFUSING_MODES = (MODE_DISABLED, MODE_EMERGENCY_STOPPED)


class LaneAbort(Exception):
    """A gate refused. Carries the evidence step name and the exit code."""

    def __init__(self, stage: str, reason: str, *, exit_code: int = EXIT_REFUSED):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.exit_code = exit_code


class LaneLockHeld(Exception):
    """Another lane process holds the lock. Carries the holder's record."""

    def __init__(self, lock_path: Path, holder: str) -> None:
        super().__init__(f"solana lane lock held: {lock_path}")
        self.lock_path = lock_path
        self.holder = holder


# ----------------------------------------------------------------------
# Single-process lock
# ----------------------------------------------------------------------
def lane_lock_path(db_path: str | Path) -> Path:
    """Lock file for the Solana lane, beside the database it guards.

    Distinct from the Kraken pilot's lock: the two lanes hold different assets
    at different venues, and neither's envelope bounds the other's exposure.
    """
    return Path(f"{db_path}.solana_lane.lock")


def acquire_lane_lock(db_path: str | Path) -> tuple[int, Path]:
    """Take the Solana lane's exclusive lock, or raise ``LaneLockHeld``.

    Two lane runs in two terminals are two swaps: each passes its own
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
    lock_path = lane_lock_path(db_path)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        try:
            holder = lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            holder = "(unreadable)"
        raise LaneLockHeld(lock_path, holder or "(empty)") from exc
    os.write(
        fd,
        f"pid={os.getpid()} acquired_at={datetime.now(timezone.utc).isoformat()}\n".encode(),
    )
    return fd, lock_path


def release_lane_lock(fd: int, lock_path: Path) -> None:
    """Close and remove the lock. Safe to call on a partially-taken lock."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        lock_path.unlink()
    except OSError:
        log.warning("solana_lane_lock_not_removed", lock_path=str(lock_path))


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
    """Append-only JSON-Lines evidence file for one lane decision.

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
        log.info("solana_lane_step", step=step, **_scrub(fields))
        return entry


def probe_keyfile_custody(settings: Settings) -> dict[str, Any]:
    """Verify the key file's custody WITHOUT reading a byte of it.

    Mode and ownership are properties of the directory entry, so ``stat``
    answers the custody question completely and ``open`` is never needed. That
    matters because this runs before the operator has authorized anything, and
    the rule is that the funded key is not read until after.

    Raises:
        SolanaKeypairError: missing file, permissions wider than 0600, another
            owner, or a platform where neither can be checked.
    """
    raw = (settings.SOLANA_PILOT_KEYPAIR_PATH or "").strip()
    if not raw:
        raise SolanaKeypairError(
            "SOLANA_PILOT_KEYPAIR_PATH is unset. The Solana lane refuses to "
            "run without an explicitly configured key file."
        )
    resolved = Path(raw)
    if not resolved.is_file():
        raise SolanaKeypairError(f"keypair file not found: {resolved}")
    if not hasattr(os, "getuid"):
        # Same refusal as `signer.load_keypair`: Windows reports synthetic
        # POSIX mode bits and no meaningful uid, so the guarantee would be
        # absent exactly where nobody looks for it.
        raise SolanaKeypairError(
            "refusing to use a signing key on a non-POSIX platform: key-file "
            "permissions and ownership cannot be verified. Run the Solana "
            "lane on the Linux host."
        )
    info = resolved.stat()
    enforce_keyfile_security(
        mode=info.st_mode,
        file_uid=info.st_uid,
        current_uid=os.getuid(),
        path=str(resolved),
    )
    return {
        "custody_verified": True,
        "signing_key_file": str(resolved),
        "key_material_read": False,
    }


def keyfile_public_half(settings: Settings) -> tuple[str | None, str]:
    """The key file's PUBLIC key, derived without constructing a signer.

    A Solana ``id.json`` is 64 bytes: the 32-byte secret seed followed by the
    32-byte PUBLIC key. Only the second half is used here, and no ``Keypair``
    is ever built — so this function cannot sign anything, and neither can a
    caller holding its result.

    That distinction is why ``status`` uses this instead of the loader. Reading
    a file and holding a signer are different capabilities, and the one command
    an operator runs constantly should hold neither more than it must. The file
    IS read (there is no way to learn the public half without reading it), so
    callers are expected to say so rather than imply otherwise.

    Returns ``(pubkey_or_None, detail)``. Never raises: this is a reporting
    path, and a status command that crashes on a malformed key file is worse
    than one that describes it.
    """
    raw = (settings.SOLANA_PILOT_KEYPAIR_PATH or "").strip()
    if not raw:
        return None, "SOLANA_PILOT_KEYPAIR_PATH is unset"
    try:
        payload = json.loads(Path(raw).read_text())
    except (OSError, ValueError) as exc:
        # Deliberately does not chain the original message: a JSONDecodeError
        # renders the offending document fragment, which here is key bytes.
        return None, f"unreadable as JSON ({type(exc).__name__})"
    if not isinstance(payload, list) or len(payload) != 64:
        return None, (
            "not a 64-integer array "
            f"({type(payload).__name__} of length "
            f"{len(payload) if isinstance(payload, list) else 'n/a'})"
        )
    try:
        import base58

        return base58.b58encode(bytes(payload[32:])).decode(), "ok"
    except (TypeError, ValueError) as exc:
        return None, f"public half does not encode ({type(exc).__name__})"


def unpinned_resolver_urls(settings: Settings) -> list[str]:
    """Configured resolver endpoints that sit behind a known load balancer.

    Returned as REDACTED labels — the caller puts these in refusal messages
    and evidence files, and a provider URL carries its API key.
    """
    return [
        redact_endpoint(url)
        for url in resolver_urls(settings)
        if any(host in url.lower() for host in _ROUND_ROBIN_RPC_HOSTS)
    ]


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

    So every endpoint has to be one consistent node.
    ``SOLANA_RESOLVER_RPC_URLS`` names the pool and ``SOLANA_RESOLVER_RPC_URL``
    the single deployed one; when neither is set the main RPC URL is used, but
    only if that is not itself a known round-robin.

    Returns ``(primary_url, pinned)``. ``pinned`` is an ALL-endpoint property:
    one load-balanced member of the pool is enough to manufacture a false
    verdict, because selection is by signature and every endpoint eventually
    serves a resolution. Callers that are about to act irreversibly on a
    verdict must refuse when ``pinned`` is False; callers that only REPORT a
    verdict may proceed and say the reading is degraded.
    """
    urls = resolver_urls(settings)
    return urls[0], not unpinned_resolver_urls(settings)


@dataclass(frozen=True)
class LaneVerdict:
    """The resolver's verdict after the lane's age policy has been applied.

    Kept separate from ``ResolutionReport`` because the two answer different
    questions. The resolver answers "what does the cluster say about this
    signature RIGHT NOW", from the cluster alone. This answers "what may the
    lane ACT on", which also depends on how old the row is and therefore on
    how much the cluster's silence is worth.
    """

    verdict: str
    expiry_source: str | None = None
    reason: str | None = None
    age_seconds: float | None = None

    @property
    def clears_the_lane(self) -> bool:
        return self.verdict == "definitively_not_submitted"


def row_age_seconds(created_at: Any, *, now: datetime | None = None) -> float | None:
    """Age of a ledger row in seconds, or None if it cannot be established.

    Uses the same UTC clock that stamps ``created_at`` on insert. A system
    clock stepped BACKWARDS makes rows look younger than they are, which can
    only move a verdict toward ``unresolved`` — the row keeps blocking and a
    human looks at it. That is the safe direction, and it is why this returns
    None rather than guessing on any parse failure: an unreadable timestamp
    must not become an age that licenses retiring a row.
    """
    if not created_at:
        return None
    try:
        stamped = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return None
    if stamped.tzinfo is None:
        # Every writer in this tree stamps tz-aware UTC. A naive timestamp is
        # of unknown origin, so it does not get to establish expiry.
        return None
    reference = now or datetime.now(timezone.utc)
    return (reference - stamped).total_seconds()


def apply_age_policy(
    report: ResolutionReport,
    *,
    age_seconds: float | None,
    settings: Settings,
    had_evidence_height: bool,
    pool_downgrade: str | None = None,
) -> LaneVerdict:
    """Decide what the lane may act on, given the cluster's verdict and the age.

    Three regimes, and the bounds are what make the middle one safe.

    **Older than ``SOLANA_RESOLVER_AGE_EXPIRY_MAX_SEC``.** Absence stops being
    evidence. ``getSignatureStatuses`` with ``searchTransactionHistory`` only
    reaches as far back as the node retains ledger history, so past that
    window a transaction that LANDED reads as absent. Retiring the row on that
    would be exactly the failure the resolver exists to prevent, so any
    absence-derived verdict is forced back to ``unresolved`` with reason
    ``history_window_exceeded`` and the row waits for a human.

    **Inside the window, at least ``..._MIN_SEC`` old.** A blockhash is valid
    for at most 150 slots, so a row this old has provably expired no matter
    what height was recorded. If the signature is cleanly absent, that is
    enough to reach ``definitively_not_submitted`` from ``created_at`` alone —
    which is what lets a row resolve when its evidence file is gone. Applied
    only when the evidence height was UNAVAILABLE: a recorded height is the
    fresher, more specific fact, and if it says the transaction can still land
    then age must not overrule it.

    **Younger than ``..._MIN_SEC``.** Age proves nothing yet; whatever the
    resolver concluded from the evidence height stands.

    A ``landed`` or ``failed_on_chain`` verdict passes through untouched at any
    age. Those come from the cluster HAVING the signature, which is positive
    evidence — the history window bounds what absence means, not what presence
    means.

    ``pool_downgrade`` is the resolver pool's refusal (a split view across
    endpoints, or a corroborating endpoint that disagreed) and it is FINAL.
    Without it the age rule below would happily re-derive the definitive
    verdict the pool just refused, from the same probes the pool refused to
    trust — the age rule reads absence off the probe trail, and the pool's
    objection is about what those probes are worth.
    """
    min_sec = float(settings.SOLANA_RESOLVER_AGE_EXPIRY_MIN_SEC)
    max_sec = float(settings.SOLANA_RESOLVER_AGE_EXPIRY_MAX_SEC)

    if report.verdict in ("landed", "failed_on_chain"):
        return LaneVerdict(verdict=report.verdict, age_seconds=age_seconds)

    if pool_downgrade is not None:
        return LaneVerdict(
            verdict="unresolved", reason=pool_downgrade, age_seconds=age_seconds
        )

    if age_seconds is not None and age_seconds > max_sec:
        return LaneVerdict(
            verdict="unresolved",
            reason="history_window_exceeded",
            age_seconds=age_seconds,
        )

    if report.verdict == "definitively_not_submitted":
        return LaneVerdict(
            verdict="definitively_not_submitted",
            expiry_source="evidence_last_valid_block_height",
            age_seconds=age_seconds,
        )

    cleanly_absent = bool(report.probes) and all(
        probe.outcome == "absent" for probe in report.probes
    )
    if (
        not had_evidence_height
        and cleanly_absent
        and age_seconds is not None
        and age_seconds >= min_sec
    ):
        return LaneVerdict(
            verdict="definitively_not_submitted",
            expiry_source="row_age",
            age_seconds=age_seconds,
        )

    return LaneVerdict(verdict="unresolved", age_seconds=age_seconds)


def evidence_path_for(evidence_dir: str | Path, decision_id: str) -> Path:
    """Evidence file for one decision. One naming rule, one call site each."""
    return Path(evidence_dir) / f"solana_lane_{decision_id}.json"


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


def raw_units(amount: Decimal, *, decimals: int, symbol: str) -> int:
    """Exact raw units for a decimal amount, or ``ValueError`` if inexact.

    A fractional raw unit is not a rounding question: the amount goes into the
    quote as a raw integer, so anything below one unit would be silently
    truncated into a swap the operator did not ask for. USDC's 6 decimals make
    this MORE likely to bite than SOL's 9, not less — a price-derived amount
    with seven decimal places is an ordinary thing to type.
    """
    scaled = amount * (10**decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"{amount} {symbol} is not a whole number of raw units "
            f"(1 unit = 1e-{decimals} {symbol})"
        )
    return int(scaled)


def amount_from_raw(raw: int | None, decimals: int) -> Decimal | None:
    return None if raw is None else _dec(raw) / _dec(10**decimals)


def lamports_from_sol(sol: Decimal) -> int:
    """Exact lamports for a SOL amount, or ``ValueError`` if inexact."""
    return raw_units(sol, decimals=SOL_DECIMALS, symbol="SOL")


def raw_from_usdc(usdc: Decimal) -> int:
    """Exact raw USDC units for a USDC amount, or ``ValueError`` if inexact."""
    return raw_units(usdc, decimals=USDC_DECIMALS, symbol="USDC")


def signer_token_accounts(owner: str, *mints: str) -> tuple[str, ...]:
    """Every associated token account ``owner`` can hold for ``mints``.

    Both token programs, because a mint can live under either and the address
    differs. Used to build the inspector's per-direction create/close
    allowlists: these are the only token accounts a swap for this wallet has
    any business creating or closing, and naming them explicitly is what turns
    "the route probably wraps SOL" into a checkable statement.
    """
    return tuple(
        derive_associated_token_address(owner, mint, token_program=program)
        for mint in mints
        for program in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)
    )


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
async def ensure_lane_anchor(db: Database) -> int:
    """Return the lane anchor ``paper_trades.id``, creating it if absent.

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
            "note": "Anchor row for scout.live.solana_lane. Not a trade.",
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
                   VALUES (?, 'SOLANA-LANE', 'Solana DEX lane anchor',
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
        log.info("solana_lane_anchor_created", paper_trade_id=anchor_id)
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
    paper_trade_id: int,
    size_usd: str,
    sol_price_usdc: str | None,
) -> int:
    """Insert the 'open' ledger row carrying the expected signature.

    The row is written BEFORE the approval prompt, and at that point no
        signature exists — the funded key has not signed yet, by design. So
        ``entry_order_id`` starts NULL and is filled in by
        ``persist_expected_signature`` after signing, which is the step that must
        complete before anything can be submitted.

        That gives a durable three-state ledger, and the states are distinguishable
        precisely because submission happens strictly after the signature is
        committed:

        * no row            -> nothing was ever authorized
        * row, NULL sig     -> authorized-or-not, but PROVABLY never submitted
        * row, signature    -> may or may not have reached the block engine; the
                               §7 resolver is what decides which

        Deliberately NOT ``idempotency.record_pending_order``: that helper hardcodes
        its own column list and status, and this lane needs the row shaped around
        the signature arriving later.

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
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open', ?, ?)""",
            (
                paper_trade_id,
                _ANCHOR_TOKEN_ID,
                BASE_SYMBOL,
                VENUE,
                PAIR,
                _ANCHOR_SIGNAL_TYPE,
                size_usd,
                sol_price_usdc,
                decision_id,
                now_iso,
            ),
        )
        await db._conn.commit()
        return int(cur.lastrowid or 0)


async def persist_expected_signature(
    db: Database, live_trade_id: int, signature: str
) -> str:
    """Commit the expected signature to the row, then READ IT BACK.

    This is the boundary that makes the ledger's three states meaningful. The
    read-back is not paranoia about SQLite: it is what lets the caller submit
    knowing the idempotency key is durable, so that a crash immediately after
    the POST still leaves a row the next run can ask the cluster about. A
    submission that raced ahead of this write would be a transaction nobody
    could resolve.

    Returns the value read back, so the caller can assert on it rather than
    assume the UPDATE landed.

    Raises:
        RuntimeError: the row could not be re-read, or came back holding
            something other than the signature we wrote.
    """
    await update_live_trade(db, live_trade_id, entry_order_id=signature)
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT entry_order_id FROM live_trades WHERE id = ?", (live_trade_id,)
    )
    row = await cur.fetchone()
    stored = None if row is None else row[0]
    if stored != signature:
        raise RuntimeError(
            f"expected signature did not persist to live_trades {live_trade_id}: "
            f"wrote {signature!r}, read back {stored!r}"
        )
    return str(stored)


_EXECUTION_COLUMNS = (
    "decision_id, state, mode, live_trade_id, message_sha256, "
    "expected_signature, last_valid_block_height, amount_lamports, "
    "minimum_out_raw, detail, created_at, updated_at"
)


def _execution_row(row: Any) -> dict[str, Any]:
    keys = [c.strip() for c in _EXECUTION_COLUMNS.split(",")]
    record = {key: row[i] for i, key in enumerate(keys)}
    raw = record.get("detail")
    try:
        record["detail"] = json.loads(raw) if raw else {}
    except ValueError:
        record["detail"] = {"unparseable": True}
    return record


async def load_execution(db: Database, decision_id: str) -> dict[str, Any] | None:
    """The durable execution record, or None if this run never started."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        f"SELECT {_EXECUTION_COLUMNS} FROM solana_executions WHERE decision_id = ?",
        (decision_id,),
    )
    row = await cur.fetchone()
    return None if row is None else _execution_row(row)


async def record_execution_state(
    db: Database,
    decision_id: str,
    state: str,
    *,
    mode: str,
    **fields: Any,
) -> dict[str, Any]:
    """Move an execution to ``state``, durably, or refuse.

    Every write goes through ``assert_legal_transition``, so an illegal jump
    raises here rather than becoming a row a later reader has to interpret.
    The row is upserted on ``decision_id``: one execution, one row, updated in
    place, so recovery reads a single current state rather than replaying a
    log and hoping it is complete.

    Fields already recorded are never overwritten with NULL. A later step
    knows less about the earlier ones than the earlier ones did — the
    signature is written once and must survive every subsequent transition.
    """
    if db._conn is None or db._txn_lock is None:
        raise RuntimeError("Database not initialized.")

    existing = await load_execution(db, decision_id)
    assert_legal_transition(None if existing is None else existing["state"], state)

    now_iso = datetime.now(timezone.utc).isoformat()
    carried = {
        "live_trade_id": None,
        "message_sha256": None,
        "expected_signature": None,
        "last_valid_block_height": None,
        "amount_lamports": None,
        "minimum_out_raw": None,
    }
    if existing is not None:
        for key in carried:
            carried[key] = existing.get(key)
    for key, value in fields.items():
        if key in carried and value is not None:
            carried[key] = value

    detail = fields.get("detail")
    detail_json = json.dumps(_scrub(detail)) if detail else None
    if detail_json is None and existing is not None and existing.get("detail"):
        detail_json = json.dumps(existing["detail"])

    async with db._txn_lock:
        await db._conn.execute(
            """INSERT INTO solana_executions
                 (decision_id, state, mode, live_trade_id, message_sha256,
                  expected_signature, last_valid_block_height, amount_lamports,
                  minimum_out_raw, detail, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(decision_id) DO UPDATE SET
                 state = excluded.state,
                 mode = excluded.mode,
                 live_trade_id = excluded.live_trade_id,
                 message_sha256 = excluded.message_sha256,
                 expected_signature = excluded.expected_signature,
                 last_valid_block_height = excluded.last_valid_block_height,
                 amount_lamports = excluded.amount_lamports,
                 minimum_out_raw = excluded.minimum_out_raw,
                 detail = excluded.detail,
                 updated_at = excluded.updated_at""",
            (
                decision_id,
                state,
                mode,
                carried["live_trade_id"],
                carried["message_sha256"],
                carried["expected_signature"],
                carried["last_valid_block_height"],
                carried["amount_lamports"],
                carried["minimum_out_raw"],
                detail_json,
                (existing or {}).get("created_at") or now_iso,
                now_iso,
            ),
        )
        await db._conn.commit()

    log.info(
        "solana_lane_state",
        decision_id=decision_id,
        state=state,
        mode=mode,
        recovery=recovery_disposition(state),
    )
    return await load_execution(db, decision_id) or {}


async def fetch_recoverable_executions(db: Database) -> list[dict[str, Any]]:
    """Executions a restart must decide about, oldest first.

    Terminal states are excluded because there is nothing left to decide. What
    comes back is exactly the set of runs that were interrupted, each carrying
    the state that says whether anything was sent.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    placeholders = ", ".join("?" for _ in TERMINAL_STATES)
    cur = await db._conn.execute(
        f"SELECT {_EXECUTION_COLUMNS} FROM solana_executions "
        f"WHERE state NOT IN ({placeholders}) ORDER BY created_at",
        TERMINAL_STATES,
    )
    return [_execution_row(row) for row in await cur.fetchall()]


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


async def count_supervised_reconciled(db: Database) -> int:
    """Executions that reached ``reconciled`` under SUPERVISED_LIVE.

    The bounded-autonomy precondition a flag cannot fake. ``reconciled`` and
    not merely ``finalized``: a swap that landed but whose accounting could not
    be explained is exactly the history that should NOT count toward promoting
    the lane, and the state machine already refuses to advance such a run past
    ``finalized``.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM solana_executions WHERE state = ? AND mode = ?",
        (STATE_RECONCILED, MODE_SUPERVISED_LIVE),
    )
    return int((await cur.fetchone())[0])


async def find_incoherent_executions(db: Database) -> list[dict[str, Any]]:
    """Executions whose two axes disagree: lifecycle live, money finished.

    *** THE INVARIANT THIS EXISTS TO CATCH. ***
    ``solana_executions.state`` says where in the pipeline a transaction is;
    ``live_trades.status`` says what happened to the money. An execution left
    NON-TERMINAL while its ledger row is TERMINAL is the disagreement that
    stops the lane dead: ``execution_recovery`` keys off the execution row and
    refuses, while every message the operator saw came from the ledger row and
    said the lane was clear.

    ``COHERENT_STATUSES`` has always declared this rule and ``assert_coherent``
    has always been able to check it — but nothing called it on a write path,
    so it was a documented invariant with no enforcement. This is the query
    that makes a violation visible instead of latent.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    # Both halves are expressed as NOT IN the non-terminal sets rather than as
    # IN a list of terminal ones. An enumeration of terminal ledger statuses
    # would go stale the moment someone widened the CHECK constraint, and it
    # would go stale SILENTLY — the query would keep running and quietly stop
    # catching the case it exists for. `_BLOCKING_STATUSES` is the same
    # definition `place` blocks on, so the two cannot drift.
    #
    # The join is on live_trades.client_order_id, which is UNIQUE and therefore
    # indexed — `status` runs this on every invocation.
    execution_placeholders = ", ".join("?" for _ in TERMINAL_STATES)
    ledger_placeholders = ", ".join("?" for _ in _BLOCKING_STATUSES)
    cur = await db._conn.execute(
        f"""SELECT e.decision_id, e.state, t.status, e.live_trade_id
            FROM solana_executions e
            JOIN live_trades t ON t.client_order_id = e.decision_id
            WHERE e.state NOT IN ({execution_placeholders})
              AND t.status NOT IN ({ledger_placeholders})""",
        (*TERMINAL_STATES, *_BLOCKING_STATUSES),
    )
    return [
        {
            "decision_id": row[0],
            "execution_state": row[1],
            "ledger_status": row[2],
            "live_trade_id": row[3],
        }
        for row in await cur.fetchall()
    ]


async def fetch_lane_exposure(
    db: Database, settings: Settings, *, now: datetime | None = None
) -> LaneExposure:
    """What the lane already has on, for the concurrency and daily limits.

    Three numbers, all read from the two tables that already exist:

    * open positions — ``live_trades.status='open'`` for this venue;
    * executions in flight — non-terminal ``solana_executions`` rows;
    * notional authorized today — summed from the same ledger rows.

    The notional query is bounded to the last two days rather than scanning
    the table: every writer in this lane stamps ``created_at`` with
    ``datetime.now(timezone.utc).isoformat()``, so a lexicographic bound on
    that column is a real time bound. Two days rather than one because the
    boundary is a UTC date and the string bound is a timestamp — the extra day
    is slack, and ``notional_usd_today`` does the actual date filtering.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    reference = now or datetime.now(timezone.utc)

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM live_trades WHERE venue = ? AND status = 'open'",
        (VENUE,),
    )
    open_positions = int((await cur.fetchone())[0])

    placeholders = ", ".join("?" for _ in TERMINAL_STATES)
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM solana_executions WHERE state NOT IN ({placeholders})",
        TERMINAL_STATES,
    )
    active_executions = int((await cur.fetchone())[0])

    since = (reference - timedelta(days=2)).isoformat()
    cur = await db._conn.execute(
        "SELECT size_usd, created_at FROM live_trades "
        "WHERE venue = ? AND status != 'rejected' AND created_at >= ?",
        (VENUE, since),
    )
    rows = await cur.fetchall()
    return LaneExposure(
        open_positions=open_positions,
        active_executions=active_executions,
        notional_usd_today=notional_usd_today(
            [(r[0], r[1]) for r in rows],
            # A row whose size will not parse is counted at the per-trade
            # maximum: the largest it could legitimately have been. Bad data
            # must never widen the cap.
            unreadable_size_usd=_dec(settings.SOLANA_PILOT_MAX_ORDER_USD),
            now=reference,
        ),
    )


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


_REVERSE_ROW_COLUMNS = (
    "id, client_order_id, entry_order_id, status, venue, pair, symbol, size_usd, "
    "entry_fill_price, entry_fill_qty, exit_order_id, created_at"
)


def _reverse_row_dict(row: Any) -> dict[str, Any]:
    return {
        "live_trade_id": row[0],
        "client_order_id": row[1],
        "entry_order_id": row[2],
        "status": row[3],
        "venue": row[4],
        "pair": row[5],
        "symbol": row[6],
        "size_usd": row[7],
        "entry_fill_price": row[8],
        "entry_fill_qty": row[9],
        "exit_order_id": row[10],
        "created_at": row[11],
    }


async def fetch_live_trade_row(
    db: Database, live_trade_id: int
) -> dict[str, Any] | None:
    """One ``live_trades`` row by id, DELIBERATELY NOT FILTERED BY VENUE.

    The reverse command has to be able to tell "no such row" apart from "that
    row belongs to another venue", because those are different operator
    mistakes with different fixes — and a venue filter here would collapse
    naming a Kraken row into naming a row that does not exist. The venue
    refusal happens in ``_load_reverse_row``, where it can say what the row
    actually is.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        f"SELECT {_REVERSE_ROW_COLUMNS} FROM live_trades WHERE id = ?",
        (live_trade_id,),
    )
    row = await cur.fetchone()
    return None if row is None else _reverse_row_dict(row)


async def fetch_reverse_executions(
    db: Database, *, include_terminal: bool = False
) -> list[dict[str, Any]]:
    """Reverse-swap executions, newest last. Read-only.

    Identified by the direction recorded in ``detail`` rather than by a new
    column: the execution table already stores a JSON detail blob per run, the
    direction is written into it at the first transition, and a migration for
    one discriminator on a table this small would be a schema change in service
    of a query.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    sql = f"SELECT {_EXECUTION_COLUMNS} FROM solana_executions"
    params: tuple[Any, ...] = ()
    if not include_terminal:
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        sql += f" WHERE state NOT IN ({placeholders})"
        params = TERMINAL_STATES
    sql += " ORDER BY created_at"
    cur = await db._conn.execute(sql, params)
    rows = [_execution_row(row) for row in await cur.fetchall()]
    return [
        row
        for row in rows
        if isinstance(row.get("detail"), dict)
        and row["detail"].get("direction") == DIRECTION_USDC_TO_SOL
    ]


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
# Reverse-swap authorization binding
# ----------------------------------------------------------------------
# The forward lane binds its authorization to the transaction MESSAGE HASH and
# nothing else, and that is sufficient there: the message carries the amount,
# the route, the slippage, the fees and the blockhash, so any change to the
# trade changes the hash.
#
# A reverse swap has two facts the message does NOT carry, and both are
# load-bearing:
#
#   * WHICH LEDGER ROW is being closed, and what state that row was in. The
#     message says "spend N USDC"; it does not say "and this is the position
#     recorded as live_trades #5, which was open when you were shown it". A
#     transaction that is byte-identical is still the wrong action if the row
#     it is meant to close has since been closed, rejected or reassigned.
#   * WHETHER THE USDC ACCOUNT IS TO BE CLOSED. That option changes what the
#     inspector will accept, so leaving it out of the binding would let the
#     option be flipped between the screen the operator read and the bytes
#     that get signed.
#
# So the reverse authorization binds a DIGEST over the message hash plus the
# economics the operator was shown plus the row identity. It is strictly
# stronger than the message hash: the message hash is one of its inputs.
_REVERSE_AUTH_DOMAIN = "gecko-alpha/solana-reverse-swap/v1"
_REVERSE_AUTH_TOKEN_CHARS = 8

# Evidence step carrying the durable pre-submission record for a reverse run.
_REVERSE_INTENT_STEP = "reverse_intent_persisted"


def build_reverse_snapshot(
    *,
    message_sha256: str,
    wallet: str,
    live_trade_id: int,
    row_status: str,
    usdc_in_raw: int,
    expected_sol_out_raw: int,
    minimum_sol_out_raw: int,
    route: str,
    slippage_bps: int,
    price_impact_pct: Decimal,
    total_fee_lamports: int,
    priority_fee_lamports: int,
    jito_tip_lamports: int,
    blockhash: str | None,
    close_usdc_ata: bool,
) -> dict[str, str]:
    """Every fact the reverse authorization is bound to, canonicalized.

    Every value is rendered as a string here rather than at digest time, so
    the digest cannot change because a caller passed ``100`` where it once
    passed ``"100"``. Decimals are fixed-point (``format(d, "f")``) because
    ``str(Decimal("1E-4"))`` is exponential and two spellings of one number
    must not be two digests.
    """
    return {
        "message_sha256": message_sha256,
        "wallet": wallet,
        "live_trade_id": str(live_trade_id),
        "row_status": row_status,
        "usdc_in_raw": str(usdc_in_raw),
        "expected_sol_out_raw": str(expected_sol_out_raw),
        "minimum_sol_out_raw": str(minimum_sol_out_raw),
        "route": route,
        "slippage_bps": str(slippage_bps),
        "price_impact_pct": format(price_impact_pct, "f"),
        "total_fee_lamports": str(total_fee_lamports),
        "priority_fee_lamports": str(priority_fee_lamports),
        "jito_tip_lamports": str(jito_tip_lamports),
        "blockhash": str(blockhash),
        "close_usdc_ata": "true" if close_usdc_ata else "false",
    }


def reverse_intent_digest(snapshot: dict[str, str]) -> str:
    """sha256 over the whole snapshot, under a domain tag.

    Sorted keys so field ORDER cannot change the digest, and a domain tag so
    this digest can never collide with another one computed over a
    similar-looking mapping elsewhere in the tree.
    """
    payload = "|".join(f"{key}={snapshot[key]}" for key in sorted(snapshot))
    return hashlib.sha256(
        f"{_REVERSE_AUTH_DOMAIN}|{payload}".encode("utf-8")
    ).hexdigest()


def reverse_authorization_token(snapshot: dict[str, str]) -> str:
    """What the operator types: the first characters of the bound digest."""
    return reverse_intent_digest(snapshot)[:_REVERSE_AUTH_TOKEN_CHARS]


def snapshot_differences(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Field-by-field diff of two snapshots, for the refusal message.

    A refusal that says only "the state changed" makes the operator diff two
    screens by eye; naming the field is what turns an invalidated
    authorization into an understood one.
    """
    return [
        f"{key}: approved {before.get(key)!r}, now {after.get(key)!r}"
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


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


@dataclass(frozen=True)
class AuthorizationRequest:
    """Everything an authorization policy is allowed to see.

    Assembled once by the runner and handed to whichever policy is in force,
    so a supervised run and an autonomous one are deciding on IDENTICAL facts.
    A policy that needed extra context would be a policy making a different
    decision, and that is exactly what this type exists to prevent.
    """

    decision_id: str
    mode: str
    message_sha256: str
    signer_pubkey: str
    amount_lamports: int
    quoted_out_raw: int
    minimum_out_raw: int
    slippage_bps: int
    price_impact_pct: Decimal
    total_fee_lamports: int
    last_valid_block_height: int
    render: Callable[[], Awaitable[None]]
    # Which traversal of the pair this is. A policy that wanted to treat the
    # two differently would have to say so; none does today, and the field
    # exists so an evidence reader never has to infer it from the amounts.
    direction: str = DIRECTION_SOL_TO_USDC
    # What the operator is asked to retype, and what to call it. Forward binds
    # to the message hash and nothing else; reverse binds to a digest that has
    # the message hash inside it plus the row identity the message cannot
    # carry (see "Reverse-swap authorization binding"). Defaulting to the
    # message hash keeps the forward contract byte-identical.
    binding_digest: str | None = None
    binding_label: str = "MESSAGE SHA256"

    @property
    def expected_phrase(self) -> str:
        return (self.binding_digest or self.message_sha256)[:8]


@dataclass(frozen=True)
class AuthorizationDecision:
    """The verdict, plus how it was reached.

    ``method`` is recorded in the evidence for every run, so an auditor reading
    a pack can always tell whether a human typed the phrase or a policy
    approved it — without inferring it from the mode.
    """

    authorized: bool
    outcome: str
    method: str
    detail: dict[str, Any] = field(default_factory=dict)


class AuthorizationPolicy:
    """How this lane decides that a transaction may be signed.

    THE seam between supervised and bounded-autonomous operation, and
    deliberately the ONLY one. Everything else — quoting, building, inspection,
    simulation, limits, signing, submission, reconciliation, evidence — is
    identical in both, so promoting the lane cannot quietly change what a trade
    is allowed to be. It changes only who says yes.
    """

    method = "undefined"

    async def authorize(self, request: AuthorizationRequest) -> AuthorizationDecision:
        raise NotImplementedError


class TypedOperatorAuthorization(AuthorizationPolicy):
    """SUPERVISED_LIVE and SIMULATION_ONLY: a human types the phrase.

    Renders the decision screen, then requires the first 8 characters of the
    transaction message hash. No signature exists yet and none may — the funded
    key is not read until this returns an authorized decision.
    """

    method = "typed_operator_authorization"

    async def authorize(self, request: AuthorizationRequest) -> AuthorizationDecision:
        await request.render()
        print(
            f"Type the first 8 characters of the {request.binding_label} to "
            "authorize. Anything else — including an empty line or a closed "
            "stdin — aborts. The phrase is lowercase hex and is matched exactly."
        )
        authorized, outcome = read_authorization(request.expected_phrase)
        return AuthorizationDecision(
            authorized=authorized,
            outcome=outcome,
            method=self.method,
            detail={"expected_prefix_len": 8, "prompted": True},
        )


class BoundedAutonomousAuthorization(AuthorizationPolicy):
    """BOUNDED_AUTONOMOUS: a policy check substitutes for the typed phrase.

    It substitutes for the PROMPT and nothing else. Every limit the supervised
    path enforces has already run by the time this is asked — the request it
    receives is the same object the operator would have been shown — so this
    cannot widen what a trade may be, only decide that no human needs to
    confirm this particular one.

    The preconditions are checked by the runner before the mode is honoured at
    all (see ``_check_autonomy_preconditions``); reaching this object already
    means the lane was permitted to be autonomous.

    *** THIS POLICY REFUSES BY CONSTRUCTION, AND THAT IS THE CURRENT STATE. ***
    The execution ARCHITECTURE supports bounded autonomy today: the limits
    engine never sees the mode, there is one authorization call site rather
    than a second code path, and the preconditions are enforced against the
    ledger. What does not exist is the per-trade DECISION — the thing that
    would answer "should this specific swap happen without a human". That is
    deliberate and it is not an oversight to be tidied up later: there is no
    signal path into this lane, so there is nothing for such a policy to
    decide ON, and inventing a trading decision nobody specified would be the
    second architecture this design exists to avoid.

    So ``authorize`` returns False unconditionally. A future policy replaces
    THIS METHOD and nothing else — that is the measure of whether the seam
    works. Anyone reading this after the lane goes live should take the
    refusal at face value: the mode is reachable, the envelope is real, and
    the decision is absent.
    """

    method = "bounded_autonomous_policy"

    async def authorize(self, request: AuthorizationRequest) -> AuthorizationDecision:
        return AuthorizationDecision(
            authorized=False,
            outcome="autonomous_policy_not_implemented",
            method=self.method,
            detail={
                "prompted": False,
                "note": "the execution architecture supports autonomy, but no "
                "per-trade decision policy exists — there is no signal path "
                "into this lane for one to decide on. Refuses by construction.",
            },
        )


def authorization_policy_for(mode: str) -> AuthorizationPolicy:
    """The policy that governs a mode. One mapping, one place."""
    if mode == MODE_BOUNDED_AUTONOMOUS:
        return BoundedAutonomousAuthorization()
    return TypedOperatorAuthorization()


# ----------------------------------------------------------------------
# The exit's exemption from the daily ENTRY cap
# ----------------------------------------------------------------------
# ``SOLANA_MAX_DAILY_NOTIONAL_USD`` bounds how much this lane may COMMIT in a
# UTC day. Pointed at a reverse swap it bounds something else: whether the lane
# may CLOSE what it already holds. At the deployed figure that is not a
# hypothetical — an entry plus its exit fits in a day and a second round trip
# does not, so the check standing in front of the second exit would be the one
# meant to bound the second ENTRY. A lane that can open a position it cannot
# shut is a worse failure than a lane that spent more in a day than it meant to.
#
#     risk-increasing entry:
#         subject to daily entry/notional cap
#
#     operator-authorized exit of an exact existing position:
#         exempt from the entry cap
#         subject to separate exit safety limits
#
# What this deliberately is NOT is a direction check. `if reverse: skip the cap`
# exempts "a USDC->SOL swap" — something any caller can ask for by pointing the
# swap the other way — rather than "the close of THIS position". The exemption
# is instead PROVEN, and this function is the only place in the tree that mints
# the proof.
def operator_exit_binding(
    *, selection: dict[str, Any], mode: str
) -> OperatorExitBinding:
    """Mint the exit binding from a row ``_load_reverse_row`` has already proved.

    Every field is copied from that proof rather than derived again. By the time
    this is called the row has been refused if it belongs to another venue, if
    it is not ``open``, if it already carries an exit signature, or if it has no
    recorded entry fill — and the same function is re-run immediately before
    signing, so the facts underneath the binding are re-established after the
    approval prompt rather than assumed to have held across it.

    ``authorized_by`` is read from ``authorization_policy_for``, the SAME
    mapping that decides who will actually be asked to authorize this run. That
    is what keeps BOUNDED_AUTONOMOUS out of the exemption structurally: the
    autonomous mode stamps the binding with its own policy's method, which the
    limits engine does not honour, so an autonomous run falls through to the
    ordinary daily cap. A literal here — or a boolean parameter — would make the
    exemption something the caller states rather than something the run's own
    configuration produces.
    """
    row = selection["row"]
    scaled = selection["ledger_usdc"] * _dec(10**USDC_DECIMALS)
    return OperatorExitBinding(
        live_trade_id=int(row["live_trade_id"]),
        venue=str(row["venue"]),
        direction=DIRECTION_USDC_TO_SOL,
        # ROUNDED DOWN, never to nearest. `ledger_usdc` is a product of two
        # recorded decimals and can carry more precision than USDC has raw
        # units; rounding up would hand the exemption a raw unit the row does
        # not account for.
        remaining_usdc_raw=int(scaled.to_integral_value(rounding=ROUND_DOWN)),
        authorized_by=authorization_policy_for(mode).method,
    )


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
class LaneRunner:
    """One Solana DEX lane invocation.

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
        custody_prober: Callable[[], dict[str, Any]] | None = None,
        resolver_rpc: SolanaRpcClient | None = None,
        resolver_pool: ResolverPool | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._jupiter = jupiter
        self._rpc = rpc
        self._jito = jito
        self._ks = kill_switch
        # Two separate dependencies on purpose. The prober answers "is this
        # key file safe to use" from stat alone and runs early; the loader
        # reads key material and runs ONLY after the operator authorizes.
        # Splitting them is what makes "the funded key was never opened on a
        # refusal path" a testable claim rather than a narrative one.
        self._load_keypair = keypair_loader or (lambda: load_keypair(settings))
        self._probe_custody = custody_prober or (
            lambda: probe_keyfile_custody(settings)
        )
        # ONE envelope, built from Settings and never from the mode. Every
        # limit the lane enforces is in here, which is what makes
        # BOUNDED_AUTONOMOUS a policy change rather than a second envelope.
        self._limits = LimitsEngine(settings)
        # Both sweeps of every resolution read from ONE endpoint, which must be
        # a single consistent node — see ``resolver_endpoint``. Defaults to
        # the main client so a test (or a deployment whose single RPC is
        # already dedicated) needs no second wiring.
        self._resolver_rpc = resolver_rpc or rpc
        # Every resolution goes through the pool, including the one-endpoint
        # case. A single code path means the multi-endpoint behaviour is the
        # one that has been exercised by the time a second endpoint is added —
        # the alternative is a standby path that first runs during an incident.
        #
        # The fallback pool's label comes from the CLIENT's own url rather than
        # from config, because a caller that injects a resolver client can make
        # the two differ, and an endpoint labelled as something it does not
        # read from is worse than an unlabelled one.
        self._pool = resolver_pool or ResolverPool.from_clients(
            settings,
            [
                (
                    getattr(self._resolver_rpc, "url", settings.SOLANA_RPC_URL),
                    self._resolver_rpc,
                )
            ],
        )

    # ---------------- shared gates ----------------
    def _evidence_for(self, decision_id: str) -> EvidenceLog:
        return EvidenceLog(
            evidence_path_for(self._settings.SOLANA_PILOT_EVIDENCE_DIR, decision_id)
        )

    async def _check_envelope(
        self, *, mode: str, direction: SwapDirection = DIRECTION_FORWARD
    ) -> dict[str, Any]:
        """Step 1 — the lane is enabled, a key is configured, hosts are mainnet.

        ``direction`` changes exactly one thing here: which mints the evidence
        records as the ones this run is built around. Every refusal below is
        direction-blind, which is the point — a reverse swap does not get a
        softer envelope, it gets the same one pointed the other way.
        """
        if mode not in ALL_MODES:
            raise LaneAbort(
                "envelope_gate",
                f"SOLANA_MODE={mode!r} is not a recognised mode. One of: "
                f"{', '.join(ALL_MODES)}.",
            )
        if mode == MODE_DISABLED:
            raise LaneAbort(
                "envelope_gate",
                "SOLANA_MODE is DISABLED — the lane is off. Set it to SIMULATION_ONLY to rehearse, or SUPERVISED_LIVE for a supervised trade.",
            )
        if mode == MODE_EMERGENCY_STOPPED:
            raise LaneAbort(
                "envelope_gate",
                "SOLANA_MODE is EMERGENCY_STOPPED — every execution path is refused. This is the configuration-side twin of the kill switch; clear it deliberately, and only once the reason it was set is understood.",
            )
        autonomy: dict[str, Any] | None = None
        if mode == MODE_BOUNDED_AUTONOMOUS:
            autonomy = await self._check_autonomy_preconditions()
        if not (self._settings.SOLANA_PILOT_KEYPAIR_PATH or "").strip():
            raise LaneAbort(
                "envelope_gate",
                "SOLANA_PILOT_KEYPAIR_PATH is empty — name the id.json holding "
                "the ONE approved lane key before running the lane.",
            )
        declared = (self._settings.SOLANA_PILOT_SIGNER_PUBKEY or "").strip()
        if not declared:
            raise LaneAbort(
                "envelope_gate",
                "SOLANA_PILOT_SIGNER_PUBKEY is empty — declare the wallet's "
                "PUBLIC key. Everything before the approval prompt is built "
                "against it, and none of it may open the funded key file.",
            )
        try:
            Pubkey.from_string(declared)
        except Exception as exc:
            raise LaneAbort(
                "envelope_gate",
                f"SOLANA_PILOT_SIGNER_PUBKEY is not a valid base58 public key "
                f"({type(exc).__name__}). A malformed signer would surface as "
                "an opaque build or inspection failure much later.",
            ) from exc
        # "Mainnet only" is part of the approved envelope, and the realistic
        # way to leave it is an .env host left over from a devnet experiment.
        for field in ("SOLANA_RPC_URL", "JITO_BLOCK_ENGINE_URL"):
            url = str(getattr(self._settings, field, "")).lower()
            hit = next((m for m in _NON_MAINNET_MARKERS if m in url), None)
            if hit is not None:
                raise LaneAbort(
                    "envelope_gate",
                    f"{field} points at a non-mainnet host (contains {hit!r}). "
                    "The approved envelope is mainnet only.",
                )
        # The resolver's endpoint is an envelope property for a REAL placement,
        # not an operational detail: a swap that cannot be RESOLVED afterwards
        # is a swap whose recovery path is broken before it starts.
        #
        # It is not an envelope property for a rehearsal. A --simulate-only run
        # provably never submits, so it can never produce an ambiguous
        # submission, so the resolver is never consulted — refusing one for the
        # quality of an endpoint it will not read is a category error, and it
        # blocks the rehearsal that exists to exercise everything else. The
        # operator is told instead, because a rehearsal that passed on an
        # unpinned resolver must not read as a launch-ready lane.
        resolver_url, pinned = resolver_endpoint(self._settings)
        unpinned = unpinned_resolver_urls(self._settings)
        if not pinned and mode in EXECUTING_MODES:
            raise LaneAbort(
                "envelope_gate",
                f"the resolver pool contains {len(unpinned)} load-balanced "
                f"public endpoint(s) ({', '.join(unpinned)}). Two calls can land "
                "on two nodes, and a node that is ahead on block height while "
                "missing the signature manufactures a false "
                "'definitively_not_submitted' — which clears the lane and "
                "invites a rerun of a swap that is still in flight. Every "
                "endpoint in SOLANA_RESOLVER_RPC_URLS (or the single "
                "SOLANA_RESOLVER_RPC_URL) must be a dedicated node.",
            )
        if not pinned:
            print(
                "NOTICE: resolver endpoint is NOT pinned "
                f"({', '.join(unpinned)}); a real placement will refuse until "
                "every resolver endpoint is a dedicated node. This "
                "rehearsal proceeds because it never submits."
            )
        return {
            "keypair_path_configured": True,
            "declared_signer_pubkey": declared,
            "direction": direction.name,
            "pair_label": direction.label,
            "input_mint": direction.input_mint,
            "output_mint": direction.output_mint,
            # Labels, never URLs. The deployment's RPC endpoints carry their
            # API key in the path, and the evidence file is durable, copied
            # into review packages and read by people who are not the operator.
            "rpc_url": redact_endpoint(self._settings.SOLANA_RPC_URL),
            "resolver_rpc_url": redact_endpoint(resolver_url),
            "resolver_endpoints": list(self._pool.labels),
            "resolver_endpoint_count": self._pool.size,
            "resolver_endpoint_pinned": pinned,
            # True only on a rehearsal that would have been refused. Recorded
            # so an evidence pack can never be mistaken for one produced under
            # the full envelope.
            "resolver_pin_waived_for_rehearsal": (
                mode not in EXECUTING_MODES and not pinned
            ),
            "block_engine_url": self._settings.JITO_BLOCK_ENGINE_URL,
            "mode": mode,
            "will_execute": mode in EXECUTING_MODES,
            "authorization_method": authorization_policy_for(mode).method,
            "autonomy_preconditions": autonomy,
            "limits": self._limits.declared_limits(),
        }

    async def _check_resolver_pool(
        self, *, mode: str, evidence: EvidenceLog
    ) -> dict[str, Any]:
        """Prove every resolver endpoint is on mainnet-beta and caught up.

        The host-substring checks in the envelope gate are a typo guard: they
        catch ``devnet`` in a URL an operator pasted. This is the actual proof,
        and it is a different question — a mainnet-looking URL can serve a
        devnet node, a fork, or a node twenty thousand slots behind, and all
        three answer "absent" to signatures that exist. That answer is one half
        of the only verdict that clears the lane.

        Runs BEFORE startup reconciliation, because reconciliation is the first
        thing that reads a verdict off these endpoints.

        Refusal is scoped to executing modes and to the whole pool being
        unusable. One bad endpoint out of several is EXCLUDED, not fatal —
        degrading to fewer endpoints is the pool working, and refusing to trade
        because a spare is down would make adding a spare a liability.
        """
        health = await self._pool.check_health()
        usable = [entry for entry in health if entry.usable]
        detail = {
            "endpoints": [entry.as_evidence() for entry in health],
            "usable_count": len(usable),
            "configured_count": len(health),
            "expected_genesis": SOLANA_MAINNET_GENESIS_HASH,
        }
        # Recorded BEFORE the refusal decision. The run that gets refused here
        # is the one whose per-endpoint findings an operator most needs, and an
        # abort reason is a sentence where this is a structured record.
        evidence.record("resolver_pool", **detail)
        if usable:
            return detail

        reasons = "; ".join(f"{e.label}: {e.detail}" for e in health)
        if mode in EXECUTING_MODES:
            raise LaneAbort(
                "resolver_pool",
                "no resolver endpoint proved it is on Solana mainnet-beta and "
                f"caught up ({reasons}). A swap that cannot be RESOLVED "
                "afterwards has no recovery path, so the lane refuses before "
                "building one.",
            )
        print(
            "NOTICE: no resolver endpoint passed the genesis/health check "
            f"({reasons}). A real placement will refuse; this rehearsal "
            "proceeds because it never submits."
        )
        return detail

    async def _terminalize_execution(
        self, decision_id: str, *, reason: str, target: str = STATE_FAILED
    ) -> str | None:
        """Advance an execution to ``target``, idempotently and without raising.

        *** REACHING A DEFINITIVE VERDICT MUST END THE EXECUTION ROW. ***
        Before this existed, a definitive verdict retired the LEDGER row and
        left the EXECUTION row at ``submission_attempted`` forever. The next
        ``place`` then refused at ``execution_recovery`` — while the message
        the operator had just read said the lane was clear. That is not a
        cosmetic mismatch: it is a lane that cannot be restarted, reported as
        one that can.

        Deliberately NOT conditional on the ledger row's status. The ledger
        being already-rejected is the NORMAL case here — the in-run handler
        retires it moments earlier — so gating on "is the ledger still
        blocking" is exactly the bug.

        Never raises, because both call sites are recovery paths. A row that is
        already terminal, or whose current state cannot legally reach
        ``target``, is left alone and reported rather than crashing the command
        an operator is running to get unstuck.

        Returns the state the row ended in, or None when there is no row (a
        rehearsal writes none).
        """
        execution = await load_execution(self._db, decision_id)
        if execution is None:
            return None
        current = execution["state"]
        if current in TERMINAL_STATES:
            return current
        try:
            assert_legal_transition(current, target)
        except IllegalStateTransition as exc:
            # Reported, not raised. The row stays where it is and the operator
            # is told, which beats a traceback from `resolve`.
            log.warning(
                "solana_lane_terminalize_refused",
                decision_id=decision_id,
                current_state=current,
                target=target,
                error=str(exc),
            )
            return current
        # The row carries the mode it ran under. Reading SOLANA_MODE here would
        # attribute an old execution to whatever the config says today.
        await record_execution_state(
            self._db,
            decision_id,
            target,
            mode=execution["mode"] or self._settings.SOLANA_MODE,
            detail={"reason": reason, "terminalized_from": current},
        )
        log.info(
            "solana_lane_execution_terminalized",
            decision_id=decision_id,
            from_state=current,
            to_state=target,
            reason=reason,
        )
        return target

    async def _resolve(
        self,
        *,
        signature: str,
        last_valid_block_height: int | None,
        owner_pubkey: str | None = None,
    ) -> PoolResolution:
        """Every resolution in this module goes through here.

        One call site for the pool means the split-view and corroboration rules
        cannot be applied on one path and forgotten on another — and forgetting
        them on the ambiguity path, which is the one that runs with a
        transaction possibly in flight, would be the expensive place to do it.
        """
        return await resolve_with_pool(
            pool=self._pool,
            expected_signature=signature,
            last_valid_block_height=last_valid_block_height,
            settings=self._settings,
            owner_pubkey=owner_pubkey,
        )

    async def _state(
        self, decision_id: str, state: str, *, mode: str, **fields: Any
    ) -> None:
        """Record a durable transition. Executing modes only.

        A rehearsal writes no execution row on purpose. It has nothing to
        recover — it never signs and never submits — and a row from it would
        have to satisfy the lifecycle/money coherence rules without any money
        row to satisfy them with. Recovery is about transactions that might
        exist; rehearsals are exactly the runs where none can.
        """
        if mode not in EXECUTING_MODES:
            return
        await record_execution_state(self._db, decision_id, state, mode=mode, **fields)

    async def _recover_interrupted_executions(self) -> dict[str, Any]:
        """What earlier runs left behind, and what may be done about it.

        Read at the top of every placement. The disposition comes from the
        STATE, not from guessing: before submission nothing was sent, so the
        run is abandoned and the lane stays clear; at or after submission a
        transaction may exist and the lane blocks until the resolver settles
        it. Nothing here rebuilds or resubmits — there is no code path from a
        recovered row back to signing.
        """
        rows = await fetch_recoverable_executions(self._db)
        recovered: list[dict[str, Any]] = []
        blockers = 0
        for row in rows:
            disposition = recovery_disposition(row["state"])
            entry = {
                "decision_id": row["decision_id"],
                "state": row["state"],
                "disposition": disposition,
                "expected_signature": row["expected_signature"],
                "live_trade_id": row["live_trade_id"],
            }
            if disposition == "discard":
                # Provably nothing was sent. Retire the run so a crash at the
                # prompt does not wedge the lane forever.
                await record_execution_state(
                    self._db,
                    row["decision_id"],
                    STATE_FAILED,
                    mode=row["mode"],
                    detail={"reason": "interrupted before submission"},
                )
                await retire_row(self._db, row["live_trade_id"])
                entry["action"] = "abandoned"
            else:
                entry["action"] = "blocks"
                blockers += 1
            recovered.append(entry)
        return {"executions": recovered, "blockers": blockers}

    async def _check_autonomy_preconditions(self) -> dict[str, Any]:
        """BOUNDED_AUTONOMOUS cannot be reached by setting one value.

        *** THE TRANSITION IS MECHANICAL, NOT A CHECKLIST. ***
        Three preconditions, and each one is verified against something that
        cannot be asserted by editing a single line:

        1. **The mode.** Necessary, never sufficient.
        2. **A second explicit flag.** ``SOLANA_BOUNDED_AUTONOMOUS_ENABLED``, so
           promotion takes two deliberate acts in two places.
        3. **Recorded supervised history.** N executions that actually reached
           ``reconciled`` under ``SUPERVISED_LIVE``, counted from
           ``solana_executions``. This is the one a flag cannot fake: "we did
           the supervised trades" is a claim the database settles, and a lane
           that has never completed one under a human cannot promote itself by
           configuration alone.

        Every failure names what is missing and what would satisfy it, because
        a refusal an operator cannot act on invites working around the gate.
        """
        settings = self._settings
        detail: dict[str, Any] = {"mode": MODE_BOUNDED_AUTONOMOUS}

        if not settings.SOLANA_BOUNDED_AUTONOMOUS_ENABLED:
            raise LaneAbort(
                "envelope_gate",
                "SOLANA_MODE is BOUNDED_AUTONOMOUS but "
                "SOLANA_BOUNDED_AUTONOMOUS_ENABLED is False. The mode alone "
                "does not start autonomous execution — that takes two "
                "deliberate settings, on purpose.",
            )
        detail["enable_flag"] = True

        required = int(settings.SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS)
        completed = await count_supervised_reconciled(self._db)
        detail["supervised_reconciled"] = completed
        detail["supervised_required"] = required
        if completed < required:
            raise LaneAbort(
                "envelope_gate",
                f"BOUNDED_AUTONOMOUS requires {required} completed supervised "
                f"execution(s) recorded in solana_executions; there "
                f"{'is' if completed == 1 else 'are'} {completed}. Run the lane "
                "under SUPERVISED_LIVE until that many have reached the "
                "'reconciled' state. This is counted from the ledger and cannot "
                "be satisfied by configuration.",
            )

        # The envelope has to be CONFIGURED, not merely defaulted-into. An
        # autonomous lane running on whatever the caps happened to be is the
        # failure this precondition exists to prevent, so the limits that bound
        # a runaway are required to be present and finite.
        envelope = self._limits.declared_limits()
        missing = [
            name
            for name, value in (
                ("daily_notional_usd", settings.SOLANA_MAX_DAILY_NOTIONAL_USD),
                ("max_open_positions", settings.SOLANA_MAX_OPEN_POSITIONS),
                (
                    "max_concurrent_executions",
                    settings.SOLANA_MAX_CONCURRENT_EXECUTIONS,
                ),
                ("per_trade_max_usd", settings.SOLANA_PILOT_MAX_ORDER_USD),
                (
                    "max_total_fee_lamports",
                    settings.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS,
                ),
            )
            if not value or value <= 0
        ]
        if missing:
            raise LaneAbort(
                "envelope_gate",
                "BOUNDED_AUTONOMOUS requires a bounded envelope; these limits "
                f"are unset or non-positive: {', '.join(missing)}.",
            )
        detail["envelope"] = envelope

        # 4. **The cross-venue mandate.** The three locks above are lane-local: they
        # answer "may THIS lane run autonomously". They cannot answer "is autonomous
        # execution authorized at all, across venues", because nothing outside this
        # module is in their scope — and an operator promoting Solana while the CEX
        # path sits under a separate, differently-configured gate is exactly how two
        # autonomy postures drift apart without either being wrong on its own terms.
        #
        # STRICTLY ADDITIVE. This runs only after all three lane locks have already
        # passed, so it can refuse a promotion the lane would have allowed and can
        # never permit one the lane would have refused. Today the lane is not in
        # BOUNDED_AUTONOMOUS at all, so this path is unreachable and the check is
        # inert; what it buys is that promoting it later takes the cross-venue
        # mandate too.
        #
        # `precheck()` rather than `authorize()`: the mandate's full authorization
        # binds a TradeIntent, and this lane's unit of work is a transaction message
        # hash, not an intent. The venue-independent locks — mode, second flag,
        # bounded envelope — are the part that means anything here.
        from scout.live.mandate import (
            MODE_BOUNDED_AUTONOMOUS as MANDATE_MODE_BOUNDED_AUTONOMOUS,
        )
        from scout.live.mandate import ExecutionMandate, MandateRefused

        mandate = ExecutionMandate(settings=settings, db=self._db)
        try:
            mandate.precheck()
        except MandateRefused as exc:
            raise LaneAbort(
                "envelope_gate",
                f"the cross-venue execution mandate refuses ({exc.gate}): "
                f"{exc.message} Autonomous execution is authorized once, for every "
                "venue, not per lane.",
            ) from exc
        if mandate.mode != MANDATE_MODE_BOUNDED_AUTONOMOUS:
            raise LaneAbort(
                "envelope_gate",
                f"SOLANA_MODE is {MODE_BOUNDED_AUTONOMOUS} but "
                f"LIVE_EXECUTION_MANDATE_MODE is {mandate.mode!r}. A lane cannot be "
                "more autonomous than the cross-venue mandate that covers it.",
            )
        detail["cross_venue_mandate"] = mandate.mode

        log.info("solana_autonomy_preconditions_satisfied", **detail)
        return detail

    async def _check_kill_switch(self, stage: str) -> dict[str, Any]:
        """Steps 2 and 14 — refuse while the live kill switch is engaged."""
        state = await self._ks.is_active()
        if state is None:
            return {"kill_active": False}
        raise LaneAbort(
            stage,
            f"kill switch ACTIVE (event #{state.kill_event_id}, by "
            f"{state.triggered_by}, until {state.killed_until.isoformat()}): "
            f"{state.reason}",
        )

    def _custody_precheck(self) -> dict[str, Any]:
        """Step 3 — prove the key file is safe to use WITHOUT opening it.

        Mode and ownership come from ``stat``, so this answers the custody
        question completely while leaving the funded key unread. That is the
        point: it runs before the operator has authorized anything, and the
        rule is that the funded key is not read until after.

        A refusal here is an envelope refusal, not an escalation: nothing has
        been built and nothing sent.
        """
        try:
            detail = dict(self._probe_custody())
        except SolanaKeypairError as exc:
            raise LaneAbort("keypair_custody", str(exc)) from exc
        # The PATH is not the secret — the file's contents are, and those do
        # not reach this module at all. Named to avoid ``_SECRET_KEY_RE``,
        # which matches "keypair"; an evidence pack that cannot say which key
        # file was checked has lost real provenance for no gain.
        detail.setdefault("signing_key_file", self._settings.SOLANA_PILOT_KEYPAIR_PATH)
        detail["declared_signer_pubkey"] = (
            self._settings.SOLANA_PILOT_SIGNER_PUBKEY or ""
        ).strip()
        detail["key_material_read"] = False
        return detail

    def _load_funded_signer(self, *, expected: str) -> Keypair:
        """Read the funded key. Called ONLY after a successful authorization.

        The loaded key must be the wallet the whole run was built against —
        Jupiter built for it, the inspector checked the fee payer against it,
        the operator approved a transaction that pays from it. A key file
        that decodes to a different wallet is a configuration error that
        would otherwise surface as an opaque on-chain signature failure, so
        ``sign_transaction`` is given the declared pubkey and refuses.
        """
        try:
            keypair = self._load_keypair()
        except SolanaKeypairError as exc:
            raise LaneAbort("funded_signer", str(exc)) from exc
        loaded = str(keypair.pubkey())
        if loaded != expected:
            raise LaneAbort(
                "funded_signer",
                f"the key file decodes to {loaded}, but this run was built "
                f"and authorized for {expected}. Refusing to sign.",
            )
        return keypair

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
                # PROVABLY never submitted, and that is a fact about ordering
                # rather than a guess. The row is inserted before the approval
                # prompt with a NULL signature; the signature is written and
                # READ BACK by `persist_expected_signature`; only then can
                # `_submit_and_resolve` be reached. So a row still holding NULL
                # is one where the run ended before signing — a refusal, a
                # Ctrl+C at the prompt, a crash — and no transaction exists to
                # ask the cluster about.
                #
                # Retiring it is therefore safe, and leaving it would block the
                # lane on a row that asserts nothing. The ordering this rests on
                # is pinned by
                # test_the_signature_is_persisted_before_the_jito_call.
                row["resolution"] = {
                    "verdict": "definitively_not_submitted",
                    "detail": "row carries no signature, so signing never "
                    "completed and submission was never reachable",
                    "expiry_source": "never_signed",
                }
                row["blocking"] = False
                if auto_retire:
                    await retire_row(self._db, row["live_trade_id"])
                    row["auto_retired"] = True
                    log.info(
                        "solana_lane_unsigned_row_retired",
                        live_trade_id=row["live_trade_id"],
                    )
                else:
                    row["auto_retired"] = False
                continue

            intent = read_persisted_intent(
                self._settings.SOLANA_PILOT_EVIDENCE_DIR, decision_id
            )
            last_valid = (
                None if intent is None else intent.get("last_valid_block_height")
            )
            try:
                pooled = await self._resolve(
                    signature=str(signature), last_valid_block_height=last_valid
                )
            except Exception as exc:  # pragma: no cover - resolver swallows its own
                row["resolution"] = {
                    "verdict": "unresolved",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
                row["blocking"] = True
                blockers += 1
                continue

            report = pooled.report
            lane = apply_age_policy(
                report,
                age_seconds=row_age_seconds(row.get("created_at")),
                settings=self._settings,
                had_evidence_height=last_valid is not None,
                pool_downgrade=pooled.downgrade_reason,
            )
            row["resolution"] = _resolution_summary(report)
            row["resolution"].update(pooled.as_evidence())
            row["resolution"]["last_valid_block_height_source"] = (
                "evidence_file" if intent is not None else "unavailable"
            )
            # The verdict the lane ACTS on, and how it got there. Recorded
            # separately from the resolver's own so a reviewer can always
            # see which fact established expiry.
            row["resolution"]["lane_verdict"] = lane.verdict
            row["resolution"]["expiry_source"] = lane.expiry_source
            row["resolution"]["age_policy_reason"] = lane.reason
            row["resolution"]["row_age_seconds"] = lane.age_seconds
            if lane.clears_the_lane:
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
                        "solana_lane_row_auto_retired",
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
        self,
        *,
        amount_lamports: int,
        evidence: EvidenceLog,
        direction: SwapDirection = DIRECTION_FORWARD,
        exit_binding: OperatorExitBinding | None = None,
    ) -> tuple[SolanaQuote, datetime]:
        """Steps 5-6 — quote, then the limits engine's verdict on it.

        Every check here belongs to ``LimitsEngine.check_quote``; this method
        fetches the quote, reads the lane's current exposure out of the ledger
        and records the result. It deliberately owns no threshold of its own —
        the point of the engine is that "what is this lane allowed to do" has
        exactly one answer.

        The fetch time is returned alongside the quote because the freshness
        stage re-asks how old it is AFTER the approval prompt, and the prompt
        is unbounded operator time.

        The evidence step is written BEFORE the limits are enforced, so a
        refusal leaves the same structured record a pass would — the run that
        gets refused is the one whose numbers an operator most needs.

        ``exit_binding`` is passed straight through to the engine and recorded
        alongside its verdict. Only the reverse command supplies one, and only
        from a row it has already proved — see ``operator_exit_binding``.
        """
        quote = await self._jupiter.get_quote(
            amount=amount_lamports,
            input_mint=direction.input_mint,
            output_mint=direction.output_mint,
        )
        quoted_at = datetime.now(timezone.utc)

        exposure = await fetch_lane_exposure(self._db, self._settings)
        limits = self._limits.check_quote(
            quote,
            amount_lamports=amount_lamports,
            price_impact_pct=quote_price_impact_pct(quote),
            exposure=exposure,
            direction=direction.name,
            exit_binding=exit_binding,
        )

        in_amount = amount_from_raw(quote.in_amount, direction.input_decimals)
        out_amount = amount_from_raw(quote.out_amount, direction.output_decimals)
        min_out_amount = amount_from_raw(
            quote.min_out_amount, direction.output_decimals
        )
        detail = {
            "direction": direction.name,
            "in_amount_raw": quote.in_amount,
            "in_amount": _fmt(in_amount),
            "in_symbol": direction.input_symbol,
            "out_amount_raw": quote.out_amount,
            "out_amount": _fmt(out_amount),
            "out_symbol": direction.output_symbol,
            "min_out_amount_raw": quote.min_out_amount,
            "min_out_amount": _fmt(min_out_amount),
            "slippage_bps": quote.slippage_bps,
            "price_impact_pct": _fmt(quote_price_impact_pct(quote)),
            "price_impact_raw": quote.price_impact_pct,
            "swap_mode": quote.swap_mode,
            "route": _route_summary(quote),
            "route_steps": route_fingerprint(quote),
            "context_slot": quote.context_slot,
            "quoted_at": quoted_at.isoformat(),
            "exposure": exposure.as_evidence(),
            "limits": limits.as_evidence(),
            # Recorded whether or not it was honoured. A run whose exemption
            # was NOT granted is exactly the one whose binding an operator
            # needs to read against the refusal.
            "exit_binding": (
                None if exit_binding is None else exit_binding.as_evidence()
            ),
        }
        if not direction.is_reverse:
            # The forward run's evidence keeps the field names it has always
            # had. They are unit-specific ("_sol", "_usdc") and so cannot be
            # reused for the other direction without lying about the unit —
            # which is why the neutral names above exist rather than these
            # being renamed under every existing reader.
            detail.update(
                {
                    "in_amount_lamports": quote.in_amount,
                    "in_amount_sol": _fmt(in_amount),
                    "out_amount_usdc": _fmt(out_amount),
                    "min_out_amount_usdc": _fmt(min_out_amount),
                }
            )
        evidence.record("quote", **detail)
        self._enforce(limits, "quote_envelope")
        return quote, quoted_at

    def _enforce(self, limits: LimitsReport, stage: str) -> None:
        """Turn a failed limits report into the lane's own refusal.

        The engine raises its own domain exception so it can be used (and
        tested) without the runner; the lane translates that into ``LaneAbort``
        so every refusal reaches the operator through one path, with one exit
        code and one evidence shape.
        """
        try:
            limits.raise_if_failed()
        except SolanaLimitBreached as exc:
            raise LaneAbort(stage, str(exc)) from exc

    def _permitted_accounts(
        self, *, signer_pubkey: str, direction: SwapDirection, close_input_ata: bool
    ) -> dict[str, tuple[str, ...] | None]:
        """Which token accounts this direction may CREATE and CLOSE.

        *** ENUMERATION, NOT PATTERN-MATCHING. ***
        Neither list requires anything to be present. A route that creates
        nothing and closes nothing produces two empty observations and passes
        both checks — the wrap/unwrap pair is an ALLOWED shape, never a
        mandatory one, and making it mandatory would refuse a legitimate route
        for not looking like the one we expected.

        What the lists do is bound the damage of a shape we did not expect:

        **Closes, both directions.** Only the signer's own associated token
        accounts for the two mints in play. Before this the rule was "the rent
        must be paid to us", which is true of closing our own USDC account —
        harmless forward (the account has just been paid into, so an on-chain
        close fails on a non-empty account) and NOT harmless in reverse, where
        the same account is empty the instant the swap completes.

        **Closes, reverse specifically.** The input account is REMOVED from
        the set unless the operator has separately enabled its closure. That is
        the whole of requirement "never silently bundled": with the option off,
        a build that closes the USDC account is refused rather than signed.

        **Creates, reverse only.** Restricted to the same own-account set. The
        forward direction is deliberately left unrestricted (``None``): its
        rent is already bounded by the fee ceiling, ``tx_inspector`` docstring
        fact 8 argues explicitly against an owner rule there because a route
        may legitimately open a PDA-owned intermediate, and tightening a proven
        live path against no captured counter-example is a regression risk this
        change has no reason to take. Reverse is new, so it starts narrow.
        """
        own_input = signer_token_accounts(signer_pubkey, direction.input_mint)
        own_output = signer_token_accounts(signer_pubkey, direction.output_mint)
        if not direction.is_reverse:
            return {
                "close": own_input + own_output,
                "create": None,
            }
        return {
            # Output first: the wrapped-SOL account a reverse route typically
            # opens and closes again. The input (USDC) account joins it only
            # on the explicit, separately-displayed option.
            "close": own_output + (own_input if close_input_ata else ()),
            "create": own_input + own_output,
        }

    async def _inspect(
        self,
        *,
        build: SwapBuild,
        signer_pubkey: str,
        ata_rent_lamports: int,
        ata_rent_source: str,
        direction: SwapDirection = DIRECTION_FORWARD,
        close_input_ata: bool = False,
    ) -> tuple[VerificationReport, LimitsReport, dict[str, Any]]:
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
            log.warning("solana_lane_tip_accounts_degraded", count=len(tip_accounts))
        permitted = self._permitted_accounts(
            signer_pubkey=signer_pubkey,
            direction=direction,
            close_input_ata=close_input_ata,
        )
        report = await verify_swap_transaction(
            tx_b64=build.swap_transaction_b64,
            expected_signer=signer_pubkey,
            settings=self._settings,
            rpc_client=self._rpc,
            tip_accounts=tip_accounts,
            input_mint=direction.input_mint,
            output_mint=direction.output_mint,
            ata_rent_lamports=ata_rent_lamports,
            permitted_close_accounts=permitted["close"],
            permitted_ata_create_accounts=permitted["create"],
        )
        detail = {
            "direction": direction.name,
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
            "closed_accounts": list(report.closed_accounts),
            "closed_account_count": len(report.closed_accounts),
            "recovered_rent_lamports": report.recovered_rent_lamports,
            "permitted_close_accounts": list(permitted["close"] or ()),
            "permitted_ata_create_accounts": (
                None if permitted["create"] is None else list(permitted["create"])
            ),
            "input_ata_closure_permitted": close_input_ata,
            "input_ata_closed": any(
                account
                in set(signer_token_accounts(signer_pubkey, direction.input_mint))
                for account in report.closed_accounts
            ),
            "rpc_client_supplied": True,
        }
        # The fee ceilings, applied by the engine to what the inspector
        # re-derived from the bytes. The inspector enforces the same three on
        # its own — it has to be safe to use standalone — and both read them
        # from `FeeCeilings.from_settings`, so they cannot drift apart.
        limits = self._limits.check_transaction(report, direction=direction.name)
        detail["limits"] = limits.as_evidence()
        return report, limits, detail

    async def _ata_rent_lamports(self) -> tuple[int, str]:
        """Rent-exempt minimum for the output ATA, and where the figure came from.

        Asked of the cluster rather than hardcoded, because rent is a cluster
        parameter. The documented fallback keeps the lane runnable through an
        RPC hiccup, and the source is recorded so an evidence reader can tell
        which number the balance gate actually used.
        """
        try:
            value = await self._rpc.get_minimum_balance_for_rent_exemption(
                TOKEN_ACCOUNT_DATA_LENGTH
            )
        except Exception as exc:
            log.warning(
                "solana_lane_ata_rent_fallback",
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
        evidence: EvidenceLog,
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

        The headroom percentage and the cover comparison belong to the limits
        engine; this method performs the two RPC reads and records the result —
        before enforcing, so a refusal leaves the balances it refused on.
        """
        required, required_with_headroom = self._limits.required_lamports(
            amount_lamports=amount_lamports,
            total_fee_lamports=report.total_fee_lamports,
        )

        sol_lamports = await self._rpc.get_balance(signer_pubkey)
        usdc_raw = await self._rpc.get_token_balance(signer_pubkey, USDC_MINT)

        limits = self._limits.check_balance(
            sol_lamports=sol_lamports,
            amount_lamports=amount_lamports,
            total_fee_lamports=report.total_fee_lamports,
        )
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
            "headroom_pct": self._settings.SOLANA_BALANCE_HEADROOM_PCT,
            "limits": limits.as_evidence(),
        }
        evidence.record("balance", **detail)
        self._enforce(limits, "balance")
        return detail

    # ---------------- place ----------------
    async def place(
        self,
        *,
        sol: Decimal,
        mode: str | None = None,
    ) -> int:
        # The mode comes from CONFIG. `mode=` exists so the CLI can only
        # ever NARROW it (--simulate-only), never widen it: a flag that
        # could escalate DISABLED to SUPERVISED_LIVE would make the config
        # setting advisory.
        mode = mode or self._settings.SOLANA_MODE
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
            mode=mode,
            evidence_path=str(evidence.path),
            # Which database this run read its safety state from. Without it,
            # an evidence pack cannot distinguish "the kill switch was clear"
            # from "we were looking at the wrong database".
            db_path=str(Path(self._settings.DB_PATH).resolve()),
        )

        live_trade_id: int | None = None
        try:
            envelope = await self._check_envelope(mode=mode)
            executing = mode in EXECUTING_MODES
            evidence.record("envelope_gate", **envelope)

            kill = await self._check_kill_switch("kill_switch_check")
            evidence.record("kill_switch_check", **kill)

            custody = self._custody_precheck()
            signer_pubkey = custody["declared_signer_pubkey"]
            evidence.record("keypair_custody", **custody)

            recovery = await self._recover_interrupted_executions()
            evidence.record("execution_recovery", **recovery)
            if recovery["blockers"]:
                raise LaneAbort(
                    "execution_recovery",
                    f"{recovery['blockers']} interrupted execution(s) may "
                    "have reached the block engine. The lane is blocked until "
                    "each is resolved. NEVER rebuild — run `resolve`.",
                    exit_code=EXIT_BLOCKED,
                )

            # Immediately before startup reconciliation, which is the first
            # step that reads a VERDICT off these endpoints — and after
            # execution recovery, which decides from the database alone and
            # must not need the network to say the lane is stuck.
            await self._check_resolver_pool(mode=mode, evidence=evidence)

            reconciliation = await self._reconcile_open_rows(auto_retire=True)
            evidence.record(
                "startup_reconciliation",
                rows=reconciliation["rows"],
                blocker_count=reconciliation["blockers"],
            )
            if reconciliation["blockers"]:
                self._print_blocked(reconciliation)
                raise LaneAbort(
                    "startup_reconciliation",
                    f"{reconciliation['blockers']} unresolved signature(s) block "
                    "the lane",
                    exit_code=EXIT_BLOCKED,
                )

            quote, quoted_at = await self._quote_and_check(
                amount_lamports=amount_lamports, evidence=evidence
            )
            await self._state(
                decision_id,
                STATE_QUOTE_CREATED,
                mode=mode,
                amount_lamports=amount_lamports,
                minimum_out_raw=quote.min_out_amount,
            )

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
            await self._state(
                decision_id,
                STATE_TRANSACTION_BUILT,
                mode=mode,
                last_valid_block_height=build.last_valid_block_height,
            )
            if build.simulation_error is not None:
                raise LaneAbort(
                    "swap_built",
                    f"Jupiter reported a simulation error on its own build: "
                    f"{build.simulation_error}",
                )

            # The rent figure is read BEFORE inspection because the
            # inspector prices it into the total-fee ceiling. Asking
            # afterwards would leave the ceiling evaluated against a
            # constant rather than this cluster's actual rent parameters.
            ata_rent, ata_rent_source = await self._ata_rent_lamports()
            report, fee_limits, inspect_detail = await self._inspect(
                build=build,
                signer_pubkey=signer_pubkey,
                ata_rent_lamports=ata_rent,
                ata_rent_source=ata_rent_source,
            )
            evidence.record("tx_inspection", **inspect_detail)
            if not report.passed:
                raise LaneAbort(
                    "tx_inspection",
                    "; ".join(f"{c.name}: {c.detail}" for c in report.failures),
                )
            # Inspection answers "are these the bytes we asked for"; the fee
            # ceilings answer "are we allowed to spend this". A build can be
            # perfectly well-formed and still be over a ceiling.
            self._enforce(fee_limits, "tx_limits")

            balance = await self._check_balance(
                signer_pubkey=signer_pubkey,
                amount_lamports=amount_lamports,
                report=report,
                evidence=evidence,
            )

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
                raise LaneAbort(
                    "simulation",
                    f"independent simulation failed against current chain state: "
                    f"err={sim.err}. Submitting anyway would burn a fee to land "
                    "a failure.",
                )

            await self._state(decision_id, STATE_SIMULATION_PASSED, mode=mode)

            # The identity the operator is about to authorize. It is the
            # sha256 of the exact bytes the key will sign, taken from the
            # UNSIGNED transaction — no key is involved in computing it, which
            # is what lets the approval come first.
            message_sha256 = report.message_sha256

            sol_price = None
            out_usd = usdc_from_raw(quote.out_amount) or Decimal("0")
            if amount_lamports:
                sol_price = out_usd / (_dec(amount_lamports) / _dec(LAMPORTS_PER_SOL))

            if executing:
                live_trade_id = await record_solana_intent(
                    self._db,
                    decision_id=decision_id,
                    paper_trade_id=await ensure_lane_anchor(self._db),
                    size_usd=_fmt(out_usd) or "0",
                    sol_price_usdc=_fmt(sol_price),
                )
                # The durable record of WHAT IS BEING AUTHORIZED, fsynced
                # before the prompt. It carries the message hash and
                # lastValidBlockHeight but no signature — none exists yet, and
                # none will unless the operator authorizes. The row's
                # entry_order_id stays NULL until then, which is exactly what
                # makes "provably never submitted" readable off the ledger.
                evidence.record(
                    _INTENT_STEP,
                    live_trade_id=live_trade_id,
                    decision_id=decision_id,
                    message_sha256=message_sha256,
                    last_valid_block_height=build.last_valid_block_height,
                    amount_lamports=amount_lamports,
                    min_out_amount_raw=quote.min_out_amount,
                    slippage_bps=quote.slippage_bps,
                    total_fee_lamports=report.total_fee_lamports,
                    status="open",
                    entry_order_id=None,
                    note="written BEFORE the approval prompt; the signature is "
                    "added only after the operator authorizes, and submission "
                    "cannot happen until that write is confirmed",
                )
            else:
                evidence.record(
                    "intent_skipped",
                    reason="--simulate-only rehearsal never submits, so no ledger "
                    "row is written (a row would be a phantom that blocks the "
                    "next run's startup reconciliation)",
                    message_sha256=message_sha256,
                    would_be_last_valid_block_height=build.last_valid_block_height,
                )

            await self._state(
                decision_id,
                STATE_AWAITING_AUTHORIZATION,
                mode=mode,
                live_trade_id=live_trade_id,
                message_sha256=message_sha256,
            )

            # ONE call site. Which policy answers is the only thing that
            # changes between SUPERVISED_LIVE and BOUNDED_AUTONOMOUS — there is
            # deliberately no second branch through place() for autonomy to
            # diverge down.
            policy = authorization_policy_for(mode)
            request = AuthorizationRequest(
                decision_id=decision_id,
                mode=mode,
                message_sha256=message_sha256,
                signer_pubkey=signer_pubkey,
                amount_lamports=amount_lamports,
                quoted_out_raw=quote.out_amount,
                minimum_out_raw=quote.min_out_amount,
                slippage_bps=quote.slippage_bps,
                price_impact_pct=quote_price_impact_pct(quote),
                total_fee_lamports=report.total_fee_lamports,
                last_valid_block_height=build.last_valid_block_height,
                render=lambda: self._render_decision_screen(
                    decision_id=decision_id,
                    message_sha256=message_sha256,
                    signer_pubkey=signer_pubkey,
                    quote=quote,
                    build=build,
                    report=report,
                    balance=balance,
                    sim=sim,
                    mode=mode,
                ),
            )
            decision = await policy.authorize(request)
            authorized = decision.authorized
            evidence.record(
                "authorization",
                outcome="authorized" if authorized else "authorization_refused",
                detail=decision.outcome,
                method=decision.method,
                mode=mode,
                bound_to_message_sha256=message_sha256,
                funded_signer_loaded=False,
                **decision.detail,
            )
            if not authorized:
                await retire_row(self._db, live_trade_id)
                if live_trade_id is not None:
                    evidence.record(
                        "intent_retired",
                        live_trade_id=live_trade_id,
                        ledger_status="rejected",
                        note="authorization refused — no signature was ever "
                        "derived and the funded key was never read",
                    )
                raise LaneAbort(
                    "authorization",
                    f"not authorized by {decision.method} "
                    f"({decision.outcome}) — nothing was signed and nothing "
                    "was sent",
                )

            await self._state(decision_id, STATE_AUTHORIZED, mode=mode)

            if not executing:
                # The rehearsal ends HERE, before the funded key is read. A
                # signed transaction is an irreversible capability, and a
                # rehearsal has no business creating one.
                evidence.record(
                    "rehearsal_complete",
                    message_sha256=message_sha256,
                    simulation_ok=sim.ok,
                    units_consumed=sim.units_consumed,
                    funded_signer_loaded=False,
                    note="--simulate-only: the funded key was never read and "
                    "nothing was signed, so no transaction exists to submit",
                )
                _print_block(
                    "REHEARSAL COMPLETE — nothing was signed, nothing was sent",
                    [
                        f"  decision ID    : {decision_id}",
                        f"  message sha256 : {message_sha256}",
                        f"  simulation     : ok, "
                        f"{sim.units_consumed} compute units",
                        "  funded key     : NOT read (a rehearsal never signs)",
                        f"  evidence       : {evidence.path}",
                    ],
                )
                return EXIT_OK

            # Step 14 — post-approval kill re-check. Handled inline rather than
            # through LaneAbort because the intent row already exists and must
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
                raise LaneAbort(
                    "kill_switch_recheck",
                    f"kill switch engaged between approval and submission "
                    f"(event #{kill_state.kill_event_id}) — nothing was sent",
                )
            evidence.record("kill_switch_recheck", kill_active=False)

            freshness = await self._recheck_blockhash(build=build, quoted_at=quoted_at)
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
                    "AUTHORIZATION INVALIDATED — nothing was signed or sent",
                    [
                        f"  {freshness['detail']}",
                        "",
                        "  The quote and the transaction you authorized are stale.",
                        "  The funded key was never read, nothing was signed and",
                        "  nothing was submitted. The ledger row is rejected.",
                        "",
                        "  Rerun the command. The next run does the whole loop",
                        "  again: a NEW quote, a NEW message hash, and a NEW",
                        "  authorization bound to that new hash.",
                        f"  evidence: {evidence.path}",
                    ],
                )
                return EXIT_REFUSED

            # ---------------------------------------------------------------
            # THE FUNDED KEY IS READ HERE, AND NOWHERE EARLIER.
            #
            # Everything above this line runs without the private key: Jupiter
            # built for the declared pubkey, the inspector checked the fee
            # payer against it, the simulation ran on unsigned bytes, and the
            # operator authorized a message hash. A refusal at any of those
            # gates leaves the key file unopened.
            # ---------------------------------------------------------------
            try:
                keypair = self._load_funded_signer(expected=signer_pubkey)
            except LaneAbort:
                # Retire inline rather than leaving it to a later run's
                # unsigned-row sweep. Every other refusal leaves its row
                # terminal on its own, and a rule that depends on a
                # subsequent run is harder to reason about — particularly
                # when that rule is itself an inference from ordering.
                await retire_row(self._db, live_trade_id)
                if live_trade_id is not None:
                    evidence.record(
                        "intent_retired",
                        live_trade_id=live_trade_id,
                        ledger_status="rejected",
                        note="the key file does not hold the authorized wallet; nothing was signed",
                    )
                raise
            evidence.record(
                "funded_signer_loaded",
                signer_pubkey=signer_pubkey,
                after_authorization=True,
                note="read at call time, used once, dropped; never cached",
            )

            signed = sign_transaction(
                build.swap_transaction_b64, keypair, expected_signer=signer_pubkey
            )
            if signed.message_sha256 != message_sha256:
                # The bytes that were AUTHORIZED must be the bytes that were
                # signed. This is the authorization's binding made checkable:
                # the operator typed a prefix of `message_sha256`, and anything
                # that changed the message since then invalidates it.
                #
                # Retired inline for the same reason as the signer-mismatch
                # path above: this row is terminal now, not on some later run.
                await retire_row(self._db, live_trade_id)
                if live_trade_id is not None:
                    evidence.record(
                        "intent_retired",
                        live_trade_id=live_trade_id,
                        ledger_status="rejected",
                        note="signed digest did not match the authorized one; nothing was sent",
                    )
                raise LaneAbort(
                    "signed_in_memory",
                    f"signed message digest {signed.message_sha256} does not "
                    f"match the AUTHORIZED digest {message_sha256} — the "
                    "transaction changed after approval and will not be sent",
                )
            evidence.record(
                "signed_in_memory",
                expected_signature=signed.signature,
                message_sha256=signed.message_sha256,
                signer_pubkey=signed.signer_pubkey,
                matches_authorized_hash=True,
                note="signed AFTER authorization; still nothing has been sent",
            )

            # The signature must be durable before the POST, or a crash
            # immediately after submission leaves a transaction nobody can
            # resolve. `persist_expected_signature` reads it back, so this
            # returning is proof the idempotency key is on disk.
            if live_trade_id is not None:
                stored = await persist_expected_signature(
                    self._db, live_trade_id, signed.signature
                )
                evidence.record(
                    "signature_persisted",
                    live_trade_id=live_trade_id,
                    expected_signature=stored,
                    confirmed_by_read_back=True,
                    note="durable before the block engine is called; a crash "
                    "after this point leaves a resolvable row",
                )
            await self._state(
                decision_id,
                STATE_SIGNED,
                mode=mode,
                expected_signature=signed.signature,
            )

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
                mode=mode,
            )

        except LaneAbort as abort:
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
                "solana_lane_unexpected_error",
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

    async def _recheck_blockhash(
        self, *, build: SwapBuild, quoted_at: datetime | None
    ) -> dict[str, Any]:
        """Step 15 — is what the operator authorized still true?

        Two perishable things, re-asked together because they go stale for the
        same reason: the approval prompt is unbounded operator time. The
        blockhash stops being valid once the chain passes
        ``lastValidBlockHeight``, and the quote's price stops describing the
        market. The on-chain minimum-output bound protects the trade from a
        bad fill; nothing but this protects the operator's INTENT.

        An RPC failure counts as INVALID. We cannot show the transaction is
        still fresh, and the whole point of the re-check is that a stale
        authorization must not be spent.
        """
        try:
            height: int | None = await self._rpc.get_block_height()
            read_error = None
        except Exception as exc:
            height = None
            read_error = f"{type(exc).__name__}: {exc}"

        age = (
            None
            if quoted_at is None
            else (datetime.now(timezone.utc) - quoted_at).total_seconds()
        )
        limits = self._limits.check_freshness(
            quote_age_sec=age,
            current_block_height=height,
            last_valid_block_height=build.last_valid_block_height,
        )
        return {
            "valid": limits.passed,
            "current_block_height": height,
            "last_valid_block_height": build.last_valid_block_height,
            "safety_margin_blocks": int(
                self._settings.SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS
            ),
            "blocks_remaining": (
                None if height is None else build.last_valid_block_height - height
            ),
            "quote_age_sec": None if age is None else round(age, 1),
            "block_height_read_error": read_error,
            "limits": limits.as_evidence(),
            "detail": "; ".join(
                f"{c.name}: {c.detail}"
                for c in (limits.failures if limits.failures else limits.checks)
            ),
        }

    async def _render_decision_screen(
        self,
        *,
        decision_id: str,
        message_sha256: str,
        signer_pubkey: str,
        quote: SolanaQuote,
        build: SwapBuild,
        report: VerificationReport,
        balance: dict[str, Any],
        sim: Any,
        mode: str,
    ) -> None:
        """Print the decision block. Rendering only — it decides nothing.

        The phrase is the first 8 characters of the TRANSACTION MESSAGE HASH:
        the sha256 of the exact bytes the key will sign, computed from the
        UNSIGNED transaction. No signature exists at this point and none may —
        the funded key is not read until this returns True.

        The hash binds the authorization every bit as tightly as a signature
        would. Amount, route, slippage, fees, blockhash, instruction list: all
        of them are IN the message, so changing any one changes the hash and
        the operator's phrase stops matching. What it does not do is create a
        broadcastable artifact before anyone has approved one.

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
        limits = self._limits.declared_limits()
        exposure = (await fetch_lane_exposure(self._db, self._settings)).as_evidence()
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
            f"  signer              : {signer_pubkey}",
            f"  current SOL         : {balance['sol_balance']} SOL",
            f"  current USDC        : {balance['usdc_balance']} USDC",
            # The envelope this trade was judged against, shown alongside the
            # trade. An operator asked to authorize a number should not have to
            # reconstruct the bounds it cleared from a config file.
            f"  envelope            : per-trade "
            f"{limits['per_trade_usd'][0]}-{limits['per_trade_usd'][1]} USD, "
            f"daily cap {limits['daily_notional_usd']} USD "
            f"({exposure['notional_usd_today']} authorized today), "
            f"max {limits['max_open_positions']} open position(s)",
            f"  MESSAGE SHA256      : {message_sha256}",
            f"  database            : {Path(self._settings.DB_PATH).resolve()}",
            f"  decision ID         : {decision_id}",
            "  signing status      : NOT SIGNED - the funded key is read only "
            "after you authorize",
        ]
        if mode not in EXECUTING_MODES:
            lines.append(
                f"  ** REHEARSAL        : mode {mode} — the funded key is "
                "never read, nothing is signed and nothing is sent"
            )
            if not resolver_endpoint(self._settings)[1]:
                lines.append(
                    "  ** NOT LAUNCH-READY : resolver endpoint is not pinned; a "
                    "real placement will refuse"
                )
        _print_block("SOLANA DEX LANE — MANUAL APPROVAL REQUIRED", lines)

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
        mode: str,
    ) -> int:
        """Steps 16-18 — submit exactly once, then decide what happened."""
        # Durable BEFORE the POST. The whole point of this state is that a
        # process which dies mid-call has already recorded that a
        # transaction MAY exist — recording it afterwards would leave the
        # one window that matters unlogged.
        await self._state(decision_id, STATE_SUBMISSION_ATTEMPTED, mode=mode)
        # Bound on EVERY path out of the try, including the ambiguous one that
        # falls through when the resolver finds the transaction landed — there
        # is no `receipt` on that path, only the exception's copy of the id.
        submitted_bundle_id: str | None = None
        try:
            receipt = await self._jito.submit_transaction(
                signed.signed_tx_b64,
                expected_signature=signed.signature,
                last_valid_block_height=build.last_valid_block_height,
                # Not passed: the client takes SOLANA_JITO_BUNDLE_ONLY. Pinning
                # it here was how a hardcoded True survived becoming config.
            )
        except SolanaAmbiguousSubmissionError as exc:
            evidence.record(
                "submission_ambiguous",
                error_type=type(exc).__name__,
                error=str(exc),
                expected_signature=exc.expected_signature,
                note="NEVER rebuild — resolving the persisted signature instead",
                bundle_id=exc.bundle_id,
            )
            submitted_bundle_id = exc.bundle_id
            code, _resolution = await self._handle_ambiguity(
                evidence=evidence,
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                signed=signed,
                build=build,
                signer_pubkey=signer_pubkey,
                bundle_id=exc.bundle_id,
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
            submitted_bundle_id = receipt.bundle_id
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
            mode=mode,
            bundle_id=submitted_bundle_id,
        )

    async def _record_bundle_diagnostics(
        self, evidence: EvidenceLog, bundle_id: str | None
    ) -> None:
        """Record what Jito says about the bundle. Never raises, never decides.

        ``getInflightBundleStatuses`` only covers the last five minutes and
        ``getBundleStatuses`` is meaningful only for a ``bundleOnly`` send, so
        both are best-effort. An empty or failed lookup is recorded as such
        rather than swallowed — "we asked and got nothing" and "we never asked"
        are different facts, and only the first tells an operator the bundle
        was genuinely unknown to the block engine.
        """
        if not bundle_id:
            evidence.record(
                "bundle_diagnostics",
                bundle_id=None,
                available=False,
                note="no bundle id — either the response carrying x-bundle-id "
                "never arrived, or the block engine did not return one. The "
                "verdict does not depend on this.",
            )
            return
        detail: dict[str, Any] = {"bundle_id": bundle_id, "available": True}
        for label, lookup in (
            ("inflight", self._jito.get_inflight_bundle_statuses),
            ("final", self._jito.get_bundle_statuses),
        ):
            try:
                statuses = await lookup([bundle_id])
            except Exception as exc:
                detail[label] = {"error_type": type(exc).__name__}
                continue
            detail[label] = [
                {
                    "bundle_id": entry.bundle_id,
                    "status": entry.status,
                    "landed_slot": entry.landed_slot,
                    "confirmation_status": entry.confirmation_status,
                }
                for entry in statuses
            ] or "no entry returned"
        detail["note"] = (
            "diagnostic only — the verdict is decided from the signature by "
            "the resolver pool, never from bundle status"
        )
        evidence.record("bundle_diagnostics", **detail)

    async def _handle_ambiguity(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int | None,
        signed: SignedTransaction,
        build: SwapBuild,
        signer_pubkey: str,
        bundle_id: str | None = None,
    ) -> tuple[int | None, ResolutionReport]:
        """Turn an ambiguous submission into a decided state.

        Returns ``(exit_code, resolution)`` — an exit code when the run is
        over, or ``(None, resolution)`` when the transaction turns out to have
        landed and the caller should continue into the confirmation path.

        No branch here rebuilds or resubmits. A rebuild produces a different
        blockhash and a different signature, and BOTH can land — two swaps
        where the operator authorized one.
        """
        pooled = await self._resolve(
            signature=signed.signature,
            last_valid_block_height=build.last_valid_block_height,
            owner_pubkey=signer_pubkey,
        )
        resolution = pooled.report
        evidence.record(
            "ambiguity_resolution",
            **_resolution_summary(resolution),
            **pooled.as_evidence(),
        )
        # DIAGNOSTIC ONLY, recorded AFTER the verdict is already decided.
        #
        # Jito's bundle status answers "why did this not land", which is the
        # question an operator has at 2am — but it must never answer "did this
        # land", and the ordering here is what keeps those apart. A submission
        # whose response was lost entirely has no bundle id at all, which is
        # precisely the case the resolver exists for, so a verdict that leaned
        # on bundle status would be undecidable exactly when it mattered. The
        # signature is the authority; this is colour.
        await self._record_bundle_diagnostics(evidence, bundle_id)

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
            # Ledger first, then the execution row: at the instant the state is
            # written the two axes have to agree, and STATE_FAILED is coherent
            # only with a 'rejected' ledger row.
            await retire_row(self._db, live_trade_id)
            ended = await self._terminalize_execution(
                decision_id, reason="failed_on_chain"
            )
            evidence.record(
                "ambiguity_failed_on_chain",
                live_trade_id=live_trade_id,
                ledger_status="rejected",
                execution_state=ended,
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
            ended = await self._terminalize_execution(
                decision_id, reason="definitively_not_submitted"
            )
            evidence.record(
                "ambiguity_not_submitted",
                live_trade_id=live_trade_id,
                ledger_status="rejected",
                execution_state=ended,
                note="signature absent from the cluster AND its blockhash has "
                "expired, so it can never land",
            )
            # The "rerunning is safe" claim is only made when BOTH axes are
            # actually clear. It used to be printed unconditionally while the
            # execution row still blocked, so the operator was told to rerun a
            # command that would refuse.
            cleared = ended is None or ended in TERMINAL_STATES
            lines = [
                f"  signature : {signed.signature}",
                "  The signature is absent from the cluster and its blockhash",
                "  has expired, so it can never land. The ledger row is",
                "  rejected.",
                "",
            ]
            if cleared:
                lines += [
                    "  The lane is clear. Rerunning is safe: it will build a NEW",
                    "  transaction with a new signature and ask for a new",
                    "  authorization.",
                ]
            else:
                lines += [
                    f"  *** THE LANE IS NOT CLEAR: the execution row is {ended}. ***",
                    "  Do NOT rerun — `place` will refuse at execution recovery.",
                    "  Report this: a definitive verdict should have ended the",
                    "  execution row and did not.",
                ]
            lines.append(f"  evidence  : {evidence.path}")
            _print_block("SUBMISSION DID NOT LAND — no transaction exists", lines)
            return EXIT_REFUSED, resolution

        # unresolved — the dangerous one. STOP. Never rebuild.
        if live_trade_id is not None:
            await update_live_trade(
                self._db, live_trade_id, status="needs_manual_review"
            )
        await self._state(
            decision_id,
            STATE_SUBMISSION_UNKNOWN,
            mode=self._settings.SOLANA_MODE,
            detail={"reason": "resolver could not decide"},
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
                "  The lane is now BLOCKED: every future `place` run will",
                "  refuse at startup reconciliation until this row resolves.",
                "",
                "  Next steps, in order:",
                "   1. Re-run the resolver as the blockhash expires:",
                "        python -m scout.live.solana_lane resolve "
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
        it is rooted and cannot be rolled back. A supervised lane reports the
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
        mode: str,
        bundle_id: str | None = None,
    ) -> int:
        """Steps 17-18 — wait for finality, then reconcile the balances."""
        print(
            "submitted; waiting for confirmation then finalization, up to "
            f"{self._settings.SOLANA_PILOT_CONFIRM_TIMEOUT_SEC:g}s + "
            f"{self._settings.SOLANA_PILOT_FINALIZE_TIMEOUT_SEC:g}s ..."
        )
        confirmation = await self._await_confirmation(signed.signature)
        evidence.record("confirmation", **confirmation)
        if confirmation["outcome"] == "finalized":
            await self._state(decision_id, STATE_FINALIZED, mode=mode)
        elif confirmation["known"]:
            await self._state(decision_id, STATE_LANDED, mode=mode)

        if confirmation["outcome"] == "failed_on_chain":
            await self._state(
                decision_id,
                STATE_FAILED,
                mode=mode,
                detail={"reason": "failed_on_chain"},
            )
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
                bundle_id=bundle_id,
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
            f"  entry price  : "
            f"{summary['entry_economics']['executed_price_usdc_per_sol']} USDC/SOL "
            f"(quoted {summary['entry_economics']['quoted_price_usdc_per_sol']}, "
            f"slippage {summary['entry_economics']['entry_slippage_bps']} bps)",
            f"  position     : {summary['position']['wallet_usdc']} USDC in the "
            f"wallet vs {summary['position']['ledger_usdc']} claimed by the ledger",
            f"  reconcile    : {summary['verdict']}",
            f"  ledger row   : {live_trade_id} (open — the position is yours to "
            f"dispose of)",
            f"  evidence     : {evidence.path}",
        ]
        if summary["verdict"] == "review":
            lines.extend(
                ["", "  RECONCILIATION COULD NOT EXPLAIN THE BALANCE MOVE:"]
                + [f"   - {m}" for m in summary["mismatches"]]
                + ["  Check the wallet before running the lane again."]
            )
        if summary["verdict"] == "pass":
            await self._state(
                decision_id,
                STATE_RECONCILED,
                mode=mode,
                detail={"reconciliation": "pass"},
            )
        # A `review` verdict does NOT advance the state. The swap finalized but
        # did not reconcile, and `finalized` says exactly that — claiming
        # `reconciled` would assert an accounting that failed.
        _print_block(f"LANE SWAP {confirmation['outcome'].upper()}", lines)
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

        The SOL side is a RANGE rather than a point, and the range is exactly
        as wide as the one genuinely unknown term. The swap amount is exact and
        the fees are known from the inspected bytes, so "swap + fees" is the
        FLOOR — those lamports always leave. Only the ATA rent is conditional,
        recovered when an account Jupiter opened is closed again inside the
        same transaction, so "swap + fees + rent" is the ceiling. Anything
        outside is a move the runner cannot account for, and it says so.
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
        # The band's WIDTH is the ATA rent, and only the ATA rent. Rent is a
        # rent-exempt DEPOSIT rather than a fee, and the temporary wSOL account
        # Jupiter opens is closed back to the owner inside the same
        # transaction — so whether a given account's rent comes back is a
        # property of the route, not something knowable here.
        #
        # Everything else is unconditional. The base signature fee, the
        # priority fee and the Jito tip ALWAYS leave the wallet, so the floor
        # is the swap plus those, not the swap alone. Anchoring the floor at
        # the bare swap amount would have accepted a spend with zero fees —
        # impossible on-chain, and this reconciliation is the evidence the
        # slippage guarantee rests on, so it has to be exactly as tight as the
        # facts allow.
        #
        # ``report.total_fee_lamports`` already includes the rent, hence
        # subtracting it back out for the floor rather than adding it for the
        # ceiling.
        unconditional_cost = report.total_fee_lamports - ata_rent
        low = amount_lamports + unconditional_cost - slack
        high = amount_lamports + report.total_fee_lamports + slack
        if sol_spent is None:
            mismatches.append("no post-trade SOL balance to compare against")
        elif not (low <= sol_spent <= high):
            mismatches.append(
                f"SOL spent {sol_spent} lamports is outside the explained range "
                f"[{low}, {high}] (swap {amount_lamports} + unconditional "
                f"{unconditional_cost} + up to {ata_rent} of recoverable ATA "
                f"rent, ±{slack})"
            )

        _meta = (transaction or {}).get("meta") or {}
        on_chain_err = _meta.get("err")
        meta_fee = _meta.get("fee")
        if on_chain_err is not None:
            mismatches.append(f"the finalized transaction carries err={on_chain_err}")
        if confirmation["outcome"] == "finalize_timeout":
            mismatches.append(
                "the transaction confirmed but was not finalized inside the "
                "window — it is landed but not yet rooted"
            )

        # ---- the position, and what it cost to open ----
        # A balance delta says money moved. A POSITION record says what the
        # lane now holds, at what price, and how far that landed from the
        # quote the operator authorized. The second is what a later disposal
        # needs and what the daily numbers are built from, so it is written
        # into the durable row rather than only into the evidence.
        pnl = _entry_economics(
            amount_lamports=amount_lamports,
            usdc_received=usdc_received,
            quoted_out_raw=quote.out_amount,
            fee_lamports=meta_fee,
        )
        ledger = None
        if live_trade_id is not None:
            if usdc_received is not None and usdc_received > 0:
                await update_live_trade(
                    self._db,
                    live_trade_id,
                    entry_fill_qty=pnl["sol_spent"],
                    entry_fill_price=pnl["executed_price_usdc_per_sol"],
                    # Realised entry slippage against the quote the operator
                    # saw. Positive means the fill was worse than quoted.
                    # `mid_at_entry` already holds the quoted price, so the two
                    # columns are readable together.
                    entry_slippage_bps=pnl["entry_slippage_bps"],
                )
            ledger = await fetch_row_by_decision_id(self._db, decision_id)
            if ledger is None:
                mismatches.append("ledger row could not be re-read after the swap")
            elif ledger.get("entry_order_id") != signature:
                mismatches.append(
                    f"ledger entry_order_id {ledger.get('entry_order_id')!r} does "
                    f"not match the submitted signature {signature!r}"
                )

        # ---- position reconciliation ----
        # Distinct from the balance check above, which asks "did the right
        # amount move". This asks "does the wallet actually hold what the
        # ledger now says the lane holds" — the dangerous direction being a
        # ledger that asserts a position the wallet does not have, because
        # every later decision is made against the ledger.
        position = await self._reconcile_position(
            signer_pubkey=signer_pubkey, measured_usdc_raw=usdc_after
        )
        if position["shortfall_raw"] > 0:
            mismatches.append(
                f"the ledger claims {position['ledger_usdc']} USDC across "
                f"{position['open_rows']} open row(s) but the wallet holds "
                f"{position['wallet_usdc']} — short by "
                f"{position['shortfall_usdc']}"
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
            "position": position,
            "entry_economics": pnl,
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
            log.warning("solana_lane_reconciliation_review", mismatches=mismatches)
        return summary

    # ---------------- resolve ----------------
    async def resolve(self, *, decision_id: str) -> int:
        """Put one persisted signature to the cluster and report the verdict.

        Read-only except for the one safe mutation: a
        ``definitively_not_submitted`` verdict retires the row, because that
        verdict means the transaction can never land and the lane is genuinely
        clear. Every other verdict leaves the row exactly as it is.

        Deliberately NOT gated on the kill switch or on SOLANA_MODE.
        Turning the master gate off is an operator's instinctive first move
        when something looks wrong, and it must not take away the tool that
        explains what happened.
        """
        evidence = self._evidence_for(decision_id)
        evidence.record("run_started", command="resolve", decision_id=decision_id)
        try:
            row = await fetch_row_by_decision_id(self._db, decision_id)
            if row is None:
                raise LaneAbort(
                    "lookup",
                    f"no solana live_trades row with client_order_id={decision_id}",
                )
            evidence.record("ledger_row", **row)

            signature = row.get("entry_order_id")
            if not signature:
                raise LaneAbort(
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
            # The pool is validated here too, not only in `place`. This command
            # is the one that RETIRES a row on a verdict, so an endpoint on the
            # wrong chain does its damage here — and `resolve` is exactly the
            # command an operator reaches for when things already look wrong.
            pool_health = await self._pool.check_health()
            evidence.record(
                "resolver_pool",
                endpoints=[entry.as_evidence() for entry in pool_health],
                usable_count=sum(1 for entry in pool_health if entry.usable),
                configured_count=len(pool_health),
                expected_genesis=SOLANA_MAINNET_GENESIS_HASH,
            )
            pooled = await self._resolve(
                signature=str(signature), last_valid_block_height=last_valid
            )
            resolution = pooled.report
            lane = apply_age_policy(
                resolution,
                age_seconds=row_age_seconds(row.get("created_at")),
                settings=self._settings,
                had_evidence_height=last_valid is not None,
                pool_downgrade=pooled.downgrade_reason,
            )
            evidence.record(
                "resolution",
                **_resolution_summary(resolution),
                **pooled.as_evidence(),
                last_valid_block_height_source=(
                    "evidence_file" if intent is not None else "unavailable"
                ),
                resolver_rpc_url=redact_endpoint(resolver_url),
                resolver_endpoint_pinned=pinned,
                lane_verdict=lane.verdict,
                expiry_source=lane.expiry_source,
                age_policy_reason=lane.reason,
                row_age_seconds=lane.age_seconds,
            )

            # A DEFINITIVE verdict ends the run on BOTH axes.
            #
            # This used to be gated on `row["status"] in _BLOCKING_STATUSES`,
            # which skipped the whole branch whenever the in-run handler had
            # already retired the ledger row — i.e. in the normal case. The
            # execution row was then never advanced, and `place` refused at
            # recovery while `resolve` reported the lane clear.
            #
            # The pin guard applies to `definitively_not_submitted` ONLY. That
            # verdict is built from an ABSENCE, which is what a load-balanced
            # read can manufacture. `failed_on_chain` is built from the node
            # HAVING the signature with an error — presence cannot be
            # manufactured by a split read, and withholding on it would leave a
            # genuinely final outcome unresolvable, which is a new way to wedge
            # the lane rather than a protection against one.
            retired = False
            withheld = False
            execution_state = None
            definitive = lane.verdict in (
                "definitively_not_submitted",
                "failed_on_chain",
            )
            actionable = pinned or lane.verdict == "failed_on_chain"

            if definitive and actionable:
                if row["status"] in _BLOCKING_STATUSES:
                    await retire_row(
                        self._db,
                        row["live_trade_id"],
                        reject_reason=(
                            "venue_unavailable" if lane.clears_the_lane else None
                        ),
                    )
                    retired = True
                    evidence.record(
                        "row_auto_retired",
                        live_trade_id=row["live_trade_id"],
                        ledger_status="rejected",
                        verdict=lane.verdict,
                        note="the outcome is final, so the row asserts nothing "
                        "that is still true",
                    )
                # ALWAYS, regardless of what the ledger row already said.
                execution_state = await self._terminalize_execution(
                    decision_id, reason=f"resolve:{lane.verdict}"
                )
                evidence.record(
                    "execution_terminalized",
                    decision_id=decision_id,
                    execution_state=execution_state,
                    verdict=lane.verdict,
                    note="a definitive verdict ends the execution row; leaving it "
                    "non-terminal would block `place` while this command "
                    "reported the lane clear",
                )
            elif definitive and not actionable:
                withheld = True
                # Reporting a verdict is always allowed; ACTING on this one is
                # not, because a load-balanced read is exactly how a false
                # 'never submitted' is manufactured. BOTH rows stay.
                evidence.record(
                    "auto_retire_withheld",
                    live_trade_id=row["live_trade_id"],
                    resolver_rpc_url=redact_endpoint(resolver_url),
                    note="verdict read from a load-balanced endpoint; neither the "
                    "ledger row nor the execution row is advanced on it",
                )
            elif lane.verdict == "landed":
                # Not terminal — a position exists and the lane SHOULD keep
                # blocking. Advancing off `submission_attempted` is still worth
                # doing: it is what actually happened, and it moves the row onto
                # the watchdog's post-submission threshold instead of the
                # three-minute submission one.
                execution_state = await self._terminalize_execution(
                    decision_id, reason="resolve:landed", target=STATE_LANDED
                )

            lines = [
                f"  decision ID : {decision_id}",
                f"  signature   : {signature}",
                f"  row         : #{row['live_trade_id']} ({row['status']})",
                f"  verdict     : {lane.verdict}"
                + (
                    ""
                    if lane.verdict == pooled.raw_verdict
                    else f"   (cluster said {pooled.raw_verdict};"
                    f" {lane.reason or pooled.downgrade_reason})"
                ),
                f"  expiry from : {lane.expiry_source or 'not established'}",
                f"  row age     : "
                + (
                    "unknown"
                    if lane.age_seconds is None
                    else f"{lane.age_seconds / 3600:.1f}h"
                ),
                f"  slot        : {resolution.slot}",
                f"  confirmation: {resolution.confirmation_status}",
                f"  on-chain err: {resolution.on_chain_err}",
                f"  lastValidBH : {last_valid}"
                + ("" if intent is not None else "  (evidence file unreadable)"),
                f"  sweeps      : {len(resolution.probes)}",
                f"  resolver    : {pooled.endpoint} "
                f"({self._pool.size} endpoint(s) configured)"
                + ("" if pinned else "   ** NOT PINNED - verdict is advisory **"),
                f"  corroborated: {_corroboration_line(pooled)}",
                f"  execution   : {execution_state or 'unchanged'}",
                f"  evidence    : {evidence.path}",
            ]
            # "Rerunning is safe" is licensed by BOTH axes actually being
            # clear, computed after the writes — not by the verdict, and not by
            # the ledger row alone. `place` refuses on either one.
            #
            #   execution: no row at all, or a terminal one. `None` means the
            #             run predates the executions table or was a rehearsal;
            #             either way execution_recovery has nothing to block on.
            #   ledger:    retired just now, or already not blocking.
            lane_is_clear = (
                execution_state is None or execution_state in TERMINAL_STATES
            ) and (retired or row["status"] not in _BLOCKING_STATUSES)
            lines.extend(
                _verdict_guidance(lane.verdict, retired, lane_is_clear=lane_is_clear)
            )
            if lane.reason == "history_window_exceeded":
                lines.extend(
                    [
                        "",
                        "  This row is older than the node's transaction-history",
                        "  window, so 'not found' no longer means 'never landed'.",
                        "  A swap that DID execute would read exactly like this.",
                        "  The row stays blocked: check the wallet's USDC balance",
                        "  and an explorer for the signature, then dispose of the",
                        "  row by hand.",
                    ]
                )
            if withheld:
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
            _print_block("SOLANA LANE RESOLUTION", lines)
            if lane.verdict == "landed":
                return EXIT_BLOCKED
            if lane.verdict == "definitively_not_submitted":
                # EXIT_OK means "the lane is clear, go again", so it is owed to
                # exactly the fact the printed guidance is owed to. A withheld
                # verdict, or one that could not end the execution row, still
                # BLOCKS — and a 0 there would tell a wrapper script the
                # opposite of what the next `place` will do.
                return EXIT_OK if lane_is_clear else EXIT_BLOCKED
            if lane.verdict == "failed_on_chain":
                return EXIT_BLOCKED
            return EXIT_ESCALATE

        except LaneAbort as abort:
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
                "solana_lane_unexpected_error",
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

    async def _reconcile_position(
        self, *, signer_pubkey: str, measured_usdc_raw: int | None
    ) -> dict[str, Any]:
        """Does the wallet hold what the ledger says the lane holds?

        Sums the recorded position across every open lane row and compares it
        against the wallet's actual USDC. Only a SHORTFALL is a mismatch: the
        wallet holding MORE is expected — it may hold USDC this lane never
        bought — while it holding less means the ledger is asserting a position
        that is not there, and every later decision is made against the ledger.

        The measured balance is reused when the caller already has it, so this
        adds no RPC call to the happy path.
        """
        if self._db._conn is None:
            raise RuntimeError("Database not initialized.")
        cur = await self._db._conn.execute(
            "SELECT entry_fill_qty, entry_fill_price, size_usd FROM live_trades "
            "WHERE venue = ? AND status = 'open'",
            (VENUE,),
        )
        rows = await cur.fetchall()
        ledger_usdc = Decimal("0")
        for qty, price, size_usd in rows:
            # Prefer the executed numbers; fall back to the authorized size for
            # a row that has not reconciled yet. A row we cannot value at all
            # is skipped rather than counted as zero, which would understate
            # the claim and hide a shortfall.
            filled_qty = _dec_or_none(qty)
            filled_price = _dec_or_none(price)
            if filled_qty is not None and filled_price is not None:
                ledger_usdc += filled_qty * filled_price
                continue
            authorized = _dec_or_none(size_usd)
            if authorized is not None:
                ledger_usdc += authorized

        wallet_raw = measured_usdc_raw
        if wallet_raw is None:
            try:
                wallet_raw = await self._rpc.get_token_balance(signer_pubkey, USDC_MINT)
            except Exception as exc:
                log.warning(
                    "solana_position_balance_unavailable",
                    error_type=type(exc).__name__,
                )
                wallet_raw = None
        wallet_usdc = usdc_from_raw(wallet_raw)

        # An unreadable wallet balance is NOT a clean reconciliation. It is
        # reported as the full ledger claim being unverified, so the verdict
        # comes out as review rather than pass.
        shortfall = (
            ledger_usdc
            if wallet_usdc is None
            else max(Decimal("0"), ledger_usdc - wallet_usdc)
        )
        return {
            "open_rows": len(rows),
            "ledger_usdc": _fmt(ledger_usdc),
            "wallet_usdc": "unreadable" if wallet_usdc is None else _fmt(wallet_usdc),
            "wallet_usdc_raw": wallet_raw,
            "shortfall_usdc": _fmt(shortfall),
            "shortfall_raw": int(shortfall * (10**USDC_DECIMALS)),
            "matches": shortfall == 0,
        }

    # ==================================================================
    # Reverse swap: USDC -> SOL, closing ONE named ledger row
    # ==================================================================
    # Same pipeline, pointed the other way. Everything below composes the
    # gates `place` already uses — envelope, kill switch, custody, execution
    # recovery, resolver pool, quote, build, inspection, simulation, the
    # authorization seam, the signer, the state machine, the resolver — and
    # differs only where the ACTION differs: this one is named for a row that
    # already exists, and it ends by closing that row rather than opening one.
    #
    # The two dispositional differences, spelled out because they are where a
    # copied-from-`place` reflex would be wrong:
    #
    #   * A failed or never-submitted reverse swap leaves the row OPEN. The
    #     position still exists; retiring the row (which is what `place` does
    #     to its own intent row) would assert that the lane holds nothing.
    #   * The execution row's ``live_trade_id`` is left NULL on purpose, and
    #     the target row is carried in ``detail`` instead. `place`'s recovery
    #     sweep RETIRES the ledger row of any execution it discards, and that
    #     row here is a real position — a crashed rehearsal must not be able to
    #     mark it rejected.
    async def _load_reverse_row(
        self, live_trade_id: int, *, stage: str = "row_selection"
    ) -> dict[str, Any]:
        """Load the named row and prove it is a closeable Solana position.

        Run at selection AND again immediately before signing, because every
        fact here can change while the approval prompt waits.
        """
        row = await fetch_live_trade_row(self._db, live_trade_id)
        if row is None:
            raise LaneAbort(stage, f"no live_trades row with id={live_trade_id}")
        if row["venue"] != VENUE:
            raise LaneAbort(
                stage,
                f"live_trades #{live_trade_id} is a {row['venue']!r} row, not "
                f"{VENUE!r}. This command only closes Solana positions; a "
                f"{row['venue']!r} position is closed through its own lane. "
                "Naming the wrong row is the one mistake that cannot be undone "
                "by refusing later, so it is refused here.",
            )
        if row["status"] != "open":
            raise LaneAbort(
                stage,
                f"live_trades #{live_trade_id} status is {row['status']!r}, not "
                "'open'. Only an open position can be closed: a "
                "needs_manual_review row must be resolved by hand first, a "
                "rejected row asserts no position, and a closed row is already "
                "done.",
            )
        if row["exit_order_id"]:
            raise LaneAbort(
                stage,
                f"live_trades #{live_trade_id} already carries exit_order_id "
                f"{row['exit_order_id']!r}. A reverse swap for this position "
                "has already been recorded — resolve it with `reverse-status` "
                "before starting another. NEVER submit a second one.",
            )
        entry_price = _dec_or_none(row["entry_fill_price"])
        entry_qty = _dec_or_none(row["entry_fill_qty"])
        if entry_price is None or entry_qty is None:
            raise LaneAbort(
                stage,
                f"live_trades #{live_trade_id} has no recorded entry fill "
                f"(entry_fill_price={row['entry_fill_price']!r}, "
                f"entry_fill_qty={row['entry_fill_qty']!r}). Without both there "
                "is no proven position to sell and no basis to price the round "
                "trip — reconcile the entry before reversing it.",
            )
        if entry_price <= 0 or entry_qty <= 0:
            raise LaneAbort(
                stage,
                f"live_trades #{live_trade_id} records a non-positive entry fill "
                f"(price={_fmt(entry_price)} qty={_fmt(entry_qty)}) — there is "
                "nothing to sell",
            )
        # What the row says the lane HOLDS, in USDC: the SOL that went in
        # multiplied by the price it executed at. `size_usd` is the AUTHORIZED
        # figure from before the swap ran, so it is the weaker statement and is
        # only a fallback for display.
        return {
            "row": row,
            "entry_price": entry_price,
            "entry_qty": entry_qty,
            "ledger_usdc": entry_qty * entry_price,
        }

    async def _check_reverse_lane(
        self, live_trade_id: int, *, stage: str = "lane_state"
    ) -> dict[str, Any]:
        """Nothing else in this lane is outstanding.

        The named row is expected to be blocking — it is the position. Any
        OTHER blocking row means a second thing is in flight, and closing one
        position while another is unaccounted for is how two swaps end up
        sharing one authorization's worth of attention.
        """
        others = [
            row
            for row in await fetch_blocking_rows(self._db)
            if row["live_trade_id"] != live_trade_id
        ]
        if others:
            raise LaneAbort(
                stage,
                f"{len(others)} other non-terminal Solana ledger row(s) are in "
                f"flight ({', '.join(str(o['live_trade_id']) for o in others)}) "
                "— resolve them before closing this position",
                exit_code=EXIT_BLOCKED,
            )
        return {"other_blocking_rows": others}

    async def _read_reverse_balances(self, signer_pubkey: str) -> dict[str, Any]:
        """Both balances, read in the SAME order every time.

        Order matters beyond tidiness: these are two JSON-RPC calls to one URL,
        and the recovery record, the approval screen and the pre-signing
        re-read all have to be describing the same pair of numbers.
        """
        sol_lamports = await self._rpc.get_balance(signer_pubkey)
        usdc_raw = await self._rpc.get_token_balance(signer_pubkey, USDC_MINT)
        return {
            "sol_balance_lamports": sol_lamports,
            "sol_balance": _fmt(sol_from_lamports(sol_lamports)),
            "usdc_balance_raw": usdc_raw,
            "usdc_balance": _fmt(usdc_from_raw(usdc_raw)),
        }

    async def _check_reverse_balance(
        self,
        *,
        signer_pubkey: str,
        amount_raw: int,
        report: VerificationReport,
        evidence: EvidenceLog,
        stage: str = "balance",
    ) -> dict[str, Any]:
        """The reverse cover question: USDC pays the swap, SOL pays the costs.

        Recorded before it is enforced, so a refusal leaves the balances it
        refused on — same contract as the forward gate.
        """
        balances = await self._read_reverse_balances(signer_pubkey)
        limits = self._limits.check_reverse_balance(
            sol_lamports=balances["sol_balance_lamports"],
            usdc_raw=balances["usdc_balance_raw"],
            amount_raw=amount_raw,
            total_fee_lamports=report.total_fee_lamports,
        )
        detail = {
            **balances,
            "swap_usdc_raw": amount_raw,
            "swap_usdc": _fmt(usdc_from_raw(amount_raw)),
            "total_fee_lamports": report.total_fee_lamports,
            "ata_rent_lamports": report.ata_rent_lamports,
            "ata_create_count": report.ata_create_count,
            "recovered_rent_lamports": report.recovered_rent_lamports,
            "headroom_pct": self._settings.SOLANA_BALANCE_HEADROOM_PCT,
            "limits": limits.as_evidence(),
        }
        evidence.record(stage, **detail)
        self._enforce(limits, stage)
        return detail

    def _reverse_closure_lines(
        self,
        *,
        close_usdc_ata: bool,
        ata_rent_lamports: int,
        report: VerificationReport | None,
        signer_pubkey: str,
    ) -> list[str]:
        """The USDC-account closure option, as its OWN line on the screen.

        Always printed, in both states. An option that only appears when it is
        on teaches the operator that its absence means nothing, and the whole
        point of this one is that the operator can see it did not happen.
        """
        rent_sol = _fmt(sol_from_lamports(ata_rent_lamports))
        if not close_usdc_ata:
            return [
                "  USDC acct closure   : DISABLED (default) — the USDC account "
                "stays open and",
                f"                        its {ata_rent_lamports} lamports "
                f"({rent_sol} SOL) of rent stay locked in it.",
                "                        A build that tries to close it is "
                "REFUSED, not signed.",
            ]
        closed = False
        if report is not None:
            own_input = set(signer_token_accounts(signer_pubkey, USDC_MINT))
            closed = any(a in own_input for a in report.closed_accounts)
        return [
            "  USDC acct closure   : ENABLED — a SEPARATE decision from the "
            "swap, permitted",
            f"                        for this run only. Recovers exactly "
            f"{ata_rent_lamports} lamports",
            f"                        ({rent_sol} SOL) of rent to {signer_pubkey}.",
            f"                        This build closes it: "
            f"{'YES' if closed else 'NO (nothing to recover)'}",
        ]

    async def _render_reverse_decision_screen(
        self,
        *,
        decision_id: str,
        digest: str,
        message_sha256: str,
        signer_pubkey: str,
        quote: SolanaQuote,
        build: SwapBuild,
        report: VerificationReport,
        balance: dict[str, Any],
        sim: Any,
        mode: str,
        selection: dict[str, Any],
        amount_raw: int,
        close_usdc_ata: bool,
        ata_rent_lamports: int,
    ) -> None:
        """The reverse approval block. Rendering only — it decides nothing."""
        row = selection["row"]
        out_sol = sol_from_lamports(quote.out_amount)
        min_out_sol = sol_from_lamports(quote.min_out_amount)
        impact = quote_price_impact_pct(quote)
        age = await self._blockhash_age(build)
        limits = self._limits.declared_limits()
        lines = [
            f"  pair                : {DIRECTION_REVERSE.label}  "
            "(CLOSING a position)",
            f"  ledger row          : live_trades #{row['live_trade_id']} "
            f"({row['status']}), venue {row['venue']}",
            f"  row claims held     : {_fmt(selection['ledger_usdc'])} USDC "
            f"({_fmt(selection['entry_qty'])} SOL in at "
            f"{_fmt(selection['entry_price'])} USDC/SOL)",
            f"  input mint          : {quote.input_mint}",
            f"  output mint         : {quote.output_mint}",
            f"  exact input         : {_fmt(usdc_from_raw(quote.in_amount))} USDC "
            f"({quote.in_amount} raw)",
            f"  expected output     : {_fmt(out_sol)} SOL "
            f"({quote.out_amount} lamports)",
            f"  minimum acceptable  : {_fmt(min_out_sol)} SOL "
            f"({quote.min_out_amount} lamports, otherAmountThreshold; this is "
            "the bound enforced on-chain)",
            f"  slippage            : {quote.slippage_bps} bps",
            f"  price impact        : {_fmt(impact)}%",
            f"  route               : {_route_summary(quote)}",
            f"  route (full)        : {route_fingerprint(quote)}",
            f"  priority fee        : {report.priority_fee_lamports} lamports",
            f"  jito tip            : {report.jito_tip_lamports} lamports "
            f"-> {report.jito_tip_destination}",
            f"  max total fee       : {report.total_fee_lamports} lamports "
            f"(base + priority + tip + account rent, of "
            f"{self._settings.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS} allowed)",
            f"  accounts created    : {report.ata_create_count} "
            f"({report.ata_rent_lamports} lamports of rent)",
            f"  accounts closed     : {len(report.closed_accounts)} "
            f"({report.recovered_rent_lamports} lamports of rent returned) "
            f"{list(report.closed_accounts)}",
        ]
        lines += self._reverse_closure_lines(
            close_usdc_ata=close_usdc_ata,
            ata_rent_lamports=ata_rent_lamports,
            report=report,
            signer_pubkey=signer_pubkey,
        )
        lines += [
            f"  blockhash           : {report.recent_blockhash}",
            f"  blockhash age       : {age}",
            f"  simulation          : err={sim.err}, "
            f"{sim.units_consumed} compute units consumed",
            f"  signer              : {signer_pubkey}",
            f"  current SOL         : {balance['sol_balance']} SOL",
            f"  current USDC        : {balance['usdc_balance']} USDC",
            f"  envelope            : per-trade "
            f"{limits['per_trade_usd'][0]}-{limits['per_trade_usd'][1]} USD on "
            f"the USDC leg, max {limits['max_ata_creates']} account create(s)",
            f"  MESSAGE SHA256      : {message_sha256}",
            f"  APPROVAL DIGEST     : {digest}",
            "                        (message hash + amounts + full route + "
            "slippage + impact",
            "                         + fees + priority fee + tip + blockhash + "
            "wallet",
            "                         + row id + row state + the closure option)",
            f"  database            : {Path(self._settings.DB_PATH).resolve()}",
            f"  decision ID         : {decision_id}",
            "  signing status      : NOT SIGNED - the funded key is read only "
            "after you authorize",
        ]
        if mode not in EXECUTING_MODES:
            lines.append(
                f"  ** REHEARSAL        : mode {mode} — the funded key is "
                "never read, nothing is signed and nothing is sent"
            )
        _print_block(
            "SOLANA REVERSE SWAP (USDC -> SOL) — MANUAL APPROVAL REQUIRED", lines
        )

    async def _reverse_state(
        self,
        decision_id: str,
        state: str,
        *,
        mode: str,
        detail: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        """Record a transition, always carrying the reverse discriminator.

        ``record_execution_state`` REPLACES the detail blob when one is passed,
        so every reverse write merges rather than assigns — otherwise the run's
        own recovery record would be erased by the next transition, which is
        the one that matters.
        """
        merged: dict[str, Any] = {"direction": DIRECTION_USDC_TO_SOL}
        existing = await load_execution(self._db, decision_id)
        if existing is not None and isinstance(existing.get("detail"), dict):
            merged.update(existing["detail"])
        if detail:
            merged.update(detail)
        await self._state(decision_id, state, mode=mode, detail=merged, **fields)

    async def reverse_place(
        self,
        *,
        live_trade_id: int,
        usdc: Decimal,
        mode: str | None = None,
        close_usdc_ata: bool = False,
    ) -> int:
        """Close ONE named Solana ledger row with a supervised USDC->SOL swap."""
        mode = mode or self._settings.SOLANA_MODE
        decision_id = str(uuid4())
        evidence = self._evidence_for(decision_id)
        try:
            amount_raw = raw_from_usdc(usdc)
            if amount_raw <= 0:
                # The CLI converter already refuses this, but `reverse_place`
                # is a public entry point and a zero amount would otherwise
                # surface as an opaque ValueError from Jupiter three steps
                # later, after the envelope had already been recorded as
                # cleared.
                raise ValueError(f"{usdc} USDC is not a swappable amount")
        except ValueError as exc:
            evidence.record("aborted", stage="args", reason=str(exc))
            print(f"REFUSED [args]: {exc}")
            return EXIT_REFUSED

        evidence.record(
            "run_started",
            command="reverse",
            direction=DIRECTION_USDC_TO_SOL,
            decision_id=decision_id,
            live_trade_id=live_trade_id,
            usdc=_fmt(usdc),
            amount_raw=amount_raw,
            close_usdc_ata=close_usdc_ata,
            mode=mode,
            evidence_path=str(evidence.path),
            db_path=str(Path(self._settings.DB_PATH).resolve()),
        )
        try:
            return await self._reverse_place_inner(
                evidence=evidence,
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                amount_raw=amount_raw,
                mode=mode,
                close_usdc_ata=close_usdc_ata,
            )
        except LaneAbort as abort:
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
                "solana_reverse_unexpected_error",
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
                    f"  live_trades row : {live_trade_id} (left as it stands)",
                    "  Confirm the on-chain state before any further action.",
                    "  NEVER rebuild — resolve the persisted signature with",
                    "    python -m scout.live.solana_lane reverse-resolve "
                    f"--decision-id {decision_id}",
                    f"  evidence        : {evidence.path}",
                ],
            )
            return EXIT_ESCALATE

    async def _reverse_place_inner(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int,
        amount_raw: int,
        mode: str,
        close_usdc_ata: bool,
    ) -> int:
        direction = DIRECTION_REVERSE
        executing = mode in EXECUTING_MODES

        envelope = await self._check_envelope(mode=mode, direction=direction)
        evidence.record("envelope_gate", **envelope)

        # The closure option takes TWO deliberate acts. A CLI flag that could
        # switch it on by itself would be a flag that widens a safety default,
        # which is the same shape as `--simulate-only` being able to escalate a
        # DISABLED lane — refused there, refused here.
        if close_usdc_ata and not self._settings.SOLANA_REVERSE_CLOSE_USDC_ATA:
            raise LaneAbort(
                "usdc_ata_closure",
                "--close-usdc-ata was passed but SOLANA_REVERSE_CLOSE_USDC_ATA "
                "is False. Closing the USDC account is a separate decision "
                "from the swap and takes two deliberate settings, on purpose. "
                "Nothing was quoted.",
            )
        evidence.record(
            "usdc_ata_closure_option",
            requested=close_usdc_ata,
            setting_enabled=bool(self._settings.SOLANA_REVERSE_CLOSE_USDC_ATA),
            effective=close_usdc_ata,
            note="OFF means a build that closes the USDC account is refused, "
            "not silently accepted",
        )

        kill = await self._check_kill_switch("kill_switch_check")
        evidence.record("kill_switch_check", **kill)

        custody = self._custody_precheck()
        signer_pubkey = custody["declared_signer_pubkey"]
        evidence.record("keypair_custody", **custody)

        recovery = await self._recover_interrupted_executions()
        evidence.record("execution_recovery", **recovery)
        if recovery["blockers"]:
            raise LaneAbort(
                "execution_recovery",
                f"{recovery['blockers']} interrupted execution(s) may have "
                "reached the block engine. The lane is blocked until each is "
                "resolved. NEVER rebuild — run `reverse-resolve` (or `resolve` "
                "for a forward run).",
                exit_code=EXIT_BLOCKED,
            )

        await self._check_resolver_pool(mode=mode, evidence=evidence)

        selection = await self._load_reverse_row(live_trade_id)
        row = selection["row"]
        evidence.record(
            "row_selected",
            live_trade_id=live_trade_id,
            status=row["status"],
            venue=row["venue"],
            pair=row["pair"],
            symbol=row["symbol"],
            client_order_id=row["client_order_id"],
            entry_order_id=row["entry_order_id"],
            entry_fill_price=_fmt(selection["entry_price"]),
            entry_fill_qty=_fmt(selection["entry_qty"]),
            ledger_usdc=_fmt(selection["ledger_usdc"]),
            size_usd=row["size_usd"],
        )
        evidence.record("lane_state", **await self._check_reverse_lane(live_trade_id))

        # Selling MORE than the row claims would be selling USDC this lane did
        # not buy. The claim is in USDC and USDC quantity does not move with
        # price, so this is an exact bound rather than a tolerance.
        requested_usdc = usdc_from_raw(amount_raw) or Decimal("0")
        if requested_usdc > selection["ledger_usdc"]:
            raise LaneAbort(
                "amount",
                f"{_fmt(requested_usdc)} USDC exceeds the "
                f"{_fmt(selection['ledger_usdc'])} USDC that live_trades "
                f"#{live_trade_id} records as held. A reverse swap closes THIS "
                "position; selling more would be selling USDC the row does not "
                "account for.",
            )

        # Minted HERE and nowhere else: from the row `_load_reverse_row` proved,
        # bounded by the amount check immediately above, and stamped with the
        # policy that will be asked to authorize. The engine re-checks every
        # field of it against the quote — this call cannot ASK for the
        # exemption, only offer the proof of it.
        exit_binding = operator_exit_binding(selection=selection, mode=mode)
        quote, quoted_at = await self._quote_and_check(
            amount_lamports=amount_raw,
            evidence=evidence,
            direction=direction,
            exit_binding=exit_binding,
        )
        await self._reverse_state(
            decision_id,
            STATE_QUOTE_CREATED,
            mode=mode,
            amount_lamports=amount_raw,
            minimum_out_raw=quote.min_out_amount,
            detail={"live_trade_id": live_trade_id, "amount_raw": amount_raw},
        )

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
        await self._reverse_state(
            decision_id,
            STATE_TRANSACTION_BUILT,
            mode=mode,
            last_valid_block_height=build.last_valid_block_height,
        )
        if build.simulation_error is not None:
            raise LaneAbort(
                "swap_built",
                f"Jupiter reported a simulation error on its own build: "
                f"{build.simulation_error}",
            )

        ata_rent, ata_rent_source = await self._ata_rent_lamports()
        report, fee_limits, inspect_detail = await self._inspect(
            build=build,
            signer_pubkey=signer_pubkey,
            ata_rent_lamports=ata_rent,
            ata_rent_source=ata_rent_source,
            direction=direction,
            close_input_ata=close_usdc_ata,
        )
        evidence.record("tx_inspection", **inspect_detail)
        if not report.passed:
            raise LaneAbort(
                "tx_inspection",
                "; ".join(f"{c.name}: {c.detail}" for c in report.failures),
            )
        self._enforce(fee_limits, "tx_limits")

        balance = await self._check_reverse_balance(
            signer_pubkey=signer_pubkey,
            amount_raw=amount_raw,
            report=report,
            evidence=evidence,
        )

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
            raise LaneAbort(
                "simulation",
                f"independent simulation failed against current chain state: "
                f"err={sim.err}. Submitting anyway would burn a fee to land "
                "a failure.",
            )
        await self._reverse_state(decision_id, STATE_SIMULATION_PASSED, mode=mode)

        message_sha256 = report.message_sha256
        snapshot = build_reverse_snapshot(
            message_sha256=message_sha256,
            wallet=signer_pubkey,
            live_trade_id=live_trade_id,
            row_status=str(row["status"]),
            usdc_in_raw=quote.in_amount,
            expected_sol_out_raw=quote.out_amount,
            minimum_sol_out_raw=quote.min_out_amount,
            route=route_fingerprint(quote),
            slippage_bps=quote.slippage_bps,
            price_impact_pct=quote_price_impact_pct(quote),
            total_fee_lamports=report.total_fee_lamports,
            priority_fee_lamports=report.priority_fee_lamports,
            jito_tip_lamports=report.jito_tip_lamports,
            blockhash=report.recent_blockhash,
            close_usdc_ata=close_usdc_ata,
        )
        digest = reverse_intent_digest(snapshot)
        evidence.record(
            "authorization_bound",
            digest=digest,
            token=reverse_authorization_token(snapshot),
            snapshot=snapshot,
            bound_fields=sorted(snapshot),
            note="the typed token is derived from this snapshot; it is "
            "recomputed from freshly-read state immediately before signing and "
            "any difference voids the authorization. The message hash is one "
            "of the bound fields, so a rebuilt transaction cannot be signed "
            "under this approval.",
        )
        await self._reverse_state(
            decision_id,
            STATE_AWAITING_AUTHORIZATION,
            mode=mode,
            message_sha256=message_sha256,
            detail={"approval_digest": digest},
        )

        policy = authorization_policy_for(mode)
        request = AuthorizationRequest(
            decision_id=decision_id,
            mode=mode,
            message_sha256=message_sha256,
            signer_pubkey=signer_pubkey,
            amount_lamports=amount_raw,
            quoted_out_raw=quote.out_amount,
            minimum_out_raw=quote.min_out_amount,
            slippage_bps=quote.slippage_bps,
            price_impact_pct=quote_price_impact_pct(quote),
            total_fee_lamports=report.total_fee_lamports,
            last_valid_block_height=build.last_valid_block_height,
            direction=direction.name,
            binding_digest=digest,
            binding_label="APPROVAL DIGEST",
            render=lambda: self._render_reverse_decision_screen(
                decision_id=decision_id,
                digest=digest,
                message_sha256=message_sha256,
                signer_pubkey=signer_pubkey,
                quote=quote,
                build=build,
                report=report,
                balance=balance,
                sim=sim,
                mode=mode,
                selection=selection,
                amount_raw=amount_raw,
                close_usdc_ata=close_usdc_ata,
                ata_rent_lamports=ata_rent,
            ),
        )
        decision = await policy.authorize(request)
        evidence.record(
            "authorization",
            outcome="authorized" if decision.authorized else "authorization_refused",
            detail=decision.outcome,
            method=decision.method,
            mode=mode,
            bound_to_digest=digest,
            bound_to_message_sha256=message_sha256,
            funded_signer_loaded=False,
            **decision.detail,
        )
        if not decision.authorized:
            # The row is left exactly as it was. Nothing was signed and nothing
            # was sent, so the position is still open and still the operator's.
            await self._terminalize_execution(
                decision_id, reason="authorization_refused"
            )
            raise LaneAbort(
                "authorization",
                f"not authorized by {decision.method} ({decision.outcome}) — "
                "nothing was signed, nothing was sent, and live_trades "
                f"#{live_trade_id} is untouched",
            )

        await self._reverse_state(decision_id, STATE_AUTHORIZED, mode=mode)

        if not executing:
            evidence.record(
                "rehearsal_complete",
                message_sha256=message_sha256,
                approval_digest=digest,
                simulation_ok=sim.ok,
                units_consumed=sim.units_consumed,
                funded_signer_loaded=False,
                note="--simulate-only: the funded key was never read and "
                "nothing was signed, so no transaction exists to submit",
            )
            _print_block(
                "REHEARSAL COMPLETE — nothing was signed, nothing was sent",
                [
                    f"  decision ID     : {decision_id}",
                    f"  live_trades row : {live_trade_id} (still open)",
                    f"  message sha256  : {message_sha256}",
                    f"  approval digest : {digest}",
                    f"  simulation      : ok, {sim.units_consumed} compute units",
                    "  funded key      : NOT read (a rehearsal never signs)",
                    f"  evidence        : {evidence.path}",
                ],
            )
            return EXIT_OK

        # ---- post-approval, pre-signing: re-ask everything perishable ----
        kill_state = await self._ks.is_active()
        if kill_state is not None:
            await self._terminalize_execution(decision_id, reason="kill_switch")
            evidence.record(
                "kill_switch_recheck",
                kill_active=True,
                kill_event_id=kill_state.kill_event_id,
                reason=kill_state.reason,
                live_trade_id=live_trade_id,
                ledger_status=row["status"],
            )
            raise LaneAbort(
                "kill_switch_recheck",
                f"kill switch engaged between approval and signing (event "
                f"#{kill_state.kill_event_id}) — nothing was signed or sent",
            )
        evidence.record("kill_switch_recheck", kill_active=False)

        freshness = await self._recheck_blockhash(build=build, quoted_at=quoted_at)
        evidence.record("blockhash_recheck", **freshness)
        if not freshness["valid"]:
            await self._terminalize_execution(decision_id, reason="stale_build")
            _print_block(
                "AUTHORIZATION INVALIDATED — nothing was signed or sent",
                [
                    f"  {freshness['detail']}",
                    "",
                    "  The quote and the transaction you authorized are stale.",
                    "  The funded key was never read, nothing was signed and",
                    f"  nothing was submitted. live_trades #{live_trade_id} is",
                    "  untouched and still open.",
                    "",
                    "  Rerun the command. The next run does the whole loop again:",
                    "  a NEW quote, a NEW message hash, a NEW digest and a NEW",
                    "  authorization bound to it.",
                    f"  evidence: {evidence.path}",
                ],
            )
            return EXIT_REFUSED

        verified = await self._verify_reverse_before_signing(
            evidence=evidence,
            decision_id=decision_id,
            snapshot=snapshot,
            digest=digest,
            live_trade_id=live_trade_id,
            signer_pubkey=signer_pubkey,
            amount_raw=amount_raw,
            approved_balance=balance,
            quote=quote,
            report=report,
            close_usdc_ata=close_usdc_ata,
        )

        # ---------------------------------------------------------------
        # THE FUNDED KEY IS READ HERE, AND NOWHERE EARLIER.
        # ---------------------------------------------------------------
        keypair = self._load_funded_signer(expected=signer_pubkey)
        evidence.record(
            "funded_signer_loaded",
            signer_pubkey=signer_pubkey,
            after_authorization=True,
            after_state_reverification=True,
            note="read at call time, used once, dropped; never cached",
        )

        signed = sign_transaction(
            build.swap_transaction_b64, keypair, expected_signer=signer_pubkey
        )
        if signed.message_sha256 != message_sha256:
            await self._terminalize_execution(decision_id, reason="digest_mismatch")
            raise LaneAbort(
                "signed_in_memory",
                f"signed message digest {signed.message_sha256} does not match "
                f"the AUTHORIZED digest {message_sha256} — the transaction "
                "changed after approval and will not be sent",
            )

        # ---- THE RECOVERY BASELINE, fsynced before anything is sent ----
        # Self-sufficient by design: a process holding none of this run's
        # memory must be able to reconcile from this record alone, which is why
        # the pre-submit balances are in it and not only the order parameters.
        intent = {
            "decision_id": decision_id,
            "direction": DIRECTION_USDC_TO_SOL,
            "live_trade_id": live_trade_id,
            "row_status_at_signing": verified["row_status"],
            "approval_digest": digest,
            "message_sha256": message_sha256,
            "expected_signature": signed.signature,
            "signer_pubkey": signer_pubkey,
            "pre_submit_sol_lamports": verified["sol_balance_lamports"],
            "pre_submit_usdc_raw": verified["usdc_balance_raw"],
            "amount_raw": amount_raw,
            "quoted_out_lamports": quote.out_amount,
            "min_out_lamports": quote.min_out_amount,
            "total_fee_lamports": report.total_fee_lamports,
            "ata_rent_lamports": report.ata_rent_lamports,
            "recovered_rent_lamports": report.recovered_rent_lamports,
            "last_valid_block_height": build.last_valid_block_height,
            "close_usdc_ata": close_usdc_ata,
        }
        evidence.record(
            _REVERSE_INTENT_STEP,
            **intent,
            note="written and fsynced BEFORE submission; if the run dies after "
            "this record a transaction may exist — resolve it by signature "
            "with `reverse-resolve`, NEVER rebuild",
        )
        await self._reverse_state(
            decision_id,
            STATE_SIGNED,
            mode=mode,
            expected_signature=signed.signature,
            detail=intent,
        )
        # Read back from the durable store rather than trusting the write, for
        # the same reason `persist_expected_signature` does on the forward
        # lane: submission that raced ahead of this would be a transaction
        # nobody could resolve.
        stored = await load_execution(self._db, decision_id)
        if stored is None or stored.get("expected_signature") != signed.signature:
            raise LaneAbort(
                "signature_persisted",
                "the expected signature did not persist to solana_executions "
                f"{decision_id}: wrote {signed.signature!r}, read back "
                f"{None if stored is None else stored.get('expected_signature')!r}. "
                "Refusing to submit a transaction that could not be resolved "
                "afterwards.",
                exit_code=EXIT_ESCALATE,
            )
        evidence.record(
            "signature_persisted",
            decision_id=decision_id,
            expected_signature=stored["expected_signature"],
            confirmed_by_read_back=True,
        )

        return await self._submit_reverse(
            evidence=evidence,
            decision_id=decision_id,
            live_trade_id=live_trade_id,
            selection=selection,
            signed=signed,
            build=build,
            quote=quote,
            report=report,
            intent=intent,
            signer_pubkey=signer_pubkey,
            mode=mode,
        )

    async def _verify_reverse_before_signing(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        snapshot: dict[str, str],
        digest: str,
        live_trade_id: int,
        signer_pubkey: str,
        amount_raw: int,
        approved_balance: dict[str, Any],
        quote: SolanaQuote,
        report: VerificationReport,
        close_usdc_ata: bool,
    ) -> dict[str, Any]:
        """Re-read the row and BOTH balances, then RECOMPUTE the digest.

        *** THIS RUNS IMMEDIATELY BEFORE SIGNING, NOT BEFORE THE SCREEN. ***
        The approval prompt is unbounded operator time and the lane lock only
        excludes another lane process — a wallet can be moved from a phone, and
        the row can be closed by hand in another terminal, and neither takes
        our lock. Checking these before the screen would prove they were true
        when the operator started reading.

        Two comparisons, both refusals:

        * the SNAPSHOT is REBUILT — from the quote, the inspection report and a
          freshly-read row, every field derived again rather than copied — and
          re-digested. Copying the approved snapshot and overwriting one field
          would make the comparison hollow: it could only ever detect the field
          that was overwritten, so a future change that re-quoted or rebuilt
          before signing would slip straight through the check meant to catch
          exactly that.
        * the BALANCES must be unchanged AND must still cover. Balances are not
          in the digest — they are not part of what the operator approves — but
          a wallet that moved between approval and signing is a wallet whose
          state nobody approved, and re-running the cover check on stale
          numbers would be checking the wrong thing.

        A mismatch NEVER rebuilds. The row is left open and untouched.
        """
        try:
            selection = await self._load_reverse_row(
                live_trade_id, stage="pre_signing_recheck"
            )
        except LaneAbort:
            # The row stopped being a closeable open position while the
            # operator was reading. Terminalize FIRST: leaving the execution
            # non-terminal would block the lane on a run that provably never
            # signed anything.
            await self._terminalize_execution(
                decision_id, reason="row_changed_before_signing"
            )
            raise
        row = selection["row"]
        balances = await self._read_reverse_balances(signer_pubkey)
        rebuilt = build_reverse_snapshot(
            message_sha256=report.message_sha256,
            wallet=signer_pubkey,
            live_trade_id=live_trade_id,
            row_status=str(row["status"]),
            usdc_in_raw=quote.in_amount,
            expected_sol_out_raw=quote.out_amount,
            minimum_sol_out_raw=quote.min_out_amount,
            route=route_fingerprint(quote),
            slippage_bps=quote.slippage_bps,
            price_impact_pct=quote_price_impact_pct(quote),
            total_fee_lamports=report.total_fee_lamports,
            priority_fee_lamports=report.priority_fee_lamports,
            jito_tip_lamports=report.jito_tip_lamports,
            blockhash=report.recent_blockhash,
            close_usdc_ata=close_usdc_ata,
        )
        rebuilt_digest = reverse_intent_digest(rebuilt)

        limits = self._limits.check_reverse_balance(
            sol_lamports=balances["sol_balance_lamports"],
            usdc_raw=balances["usdc_balance_raw"],
            amount_raw=amount_raw,
            total_fee_lamports=report.total_fee_lamports,
        )
        balance_moves = [
            f"{key}: approved {approved_balance.get(key)!r}, now {balances[key]!r}"
            for key in ("sol_balance_lamports", "usdc_balance_raw")
            if approved_balance.get(key) != balances[key]
        ]
        differences = snapshot_differences(snapshot, rebuilt)
        detail = {
            "digest_at_approval": digest,
            "digest_now": rebuilt_digest,
            "digest_matches": rebuilt_digest == digest,
            "snapshot_differences": differences,
            "balance_moves": balance_moves,
            "row_status": str(row["status"]),
            **balances,
            "limits": limits.as_evidence(),
        }
        evidence.record("pre_signing_recheck", **detail)

        if rebuilt_digest != digest or balance_moves or not limits.passed:
            await self._terminalize_execution(
                decision_id, reason="authorization_invalidated"
            )
            reasons = (
                differences
                + balance_moves
                + [f"{c.name}: {c.detail}" for c in limits.failures]
            )
            evidence.record(
                "authorization_invalidated",
                live_trade_id=live_trade_id,
                ledger_status=str(row["status"]),
                reasons=reasons,
                note="the state the authorization was given for is no longer "
                "the state in front of us; the funded key was never read",
            )
            raise LaneAbort(
                "pre_signing_recheck",
                "the state changed between approval and signing, so the "
                "authorization is void: "
                + "; ".join(reasons)
                + f". Nothing was signed, nothing was sent, and live_trades "
                f"#{live_trade_id} is untouched. Rerun for a new quote and a "
                "new authorization — this run will NOT rebuild.",
            )
        return detail

    async def _submit_reverse(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int,
        selection: dict[str, Any],
        signed: SignedTransaction,
        build: SwapBuild,
        quote: SolanaQuote,
        report: VerificationReport,
        intent: dict[str, Any],
        signer_pubkey: str,
        mode: str,
    ) -> int:
        """Submit exactly once, then decide what happened. Never resubmits."""
        await self._reverse_state(decision_id, STATE_SUBMISSION_ATTEMPTED, mode=mode)
        bundle_id: str | None = None
        try:
            receipt = await self._jito.submit_transaction(
                signed.signed_tx_b64,
                expected_signature=signed.signature,
                last_valid_block_height=build.last_valid_block_height,
            )
        except SolanaAmbiguousSubmissionError as exc:
            evidence.record(
                "submission_ambiguous",
                error_type=type(exc).__name__,
                error=str(exc),
                expected_signature=exc.expected_signature,
                bundle_id=exc.bundle_id,
                note="NEVER rebuild — resolving the persisted signature instead",
            )
            bundle_id = exc.bundle_id
            code, _resolution = await self._handle_reverse_ambiguity(
                evidence=evidence,
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                signed=signed,
                build=build,
                signer_pubkey=signer_pubkey,
                mode=mode,
                bundle_id=bundle_id,
            )
            if code is not None:
                return code
        except SolanaAPIError as exc:
            # The block engine DECIDED. Definitive: nothing was accepted, so
            # the position is untouched and the lane is clear.
            await self._terminalize_execution(decision_id, reason="submission_refused")
            evidence.record(
                "submission_refused",
                error_type=type(exc).__name__,
                error=str(exc),
                live_trade_id=live_trade_id,
                ledger_status="open (unchanged — the position was not sold)",
            )
            print(f"REFUSED [submit]: {type(exc).__name__}: {exc}")
            print(f"evidence: {evidence.path}")
            return EXIT_REFUSED
        else:
            bundle_id = receipt.bundle_id
            evidence.record(
                "submitted",
                expected_signature=receipt.signature,
                returned_signature=receipt.returned_signature,
                bundle_id=receipt.bundle_id,
                bundle_only=receipt.bundle_only,
            )

        print(
            "submitted; waiting for confirmation then finalization, up to "
            f"{self._settings.SOLANA_PILOT_CONFIRM_TIMEOUT_SEC:g}s + "
            f"{self._settings.SOLANA_PILOT_FINALIZE_TIMEOUT_SEC:g}s ..."
        )
        confirmation = await self._await_confirmation(signed.signature)
        evidence.record("confirmation", **confirmation)
        if confirmation["outcome"] == "finalized":
            await self._reverse_state(decision_id, STATE_FINALIZED, mode=mode)
        elif confirmation["known"]:
            await self._reverse_state(decision_id, STATE_LANDED, mode=mode)

        if confirmation["outcome"] == "failed_on_chain":
            await self._reverse_state(
                decision_id,
                STATE_FAILED,
                mode=mode,
                detail={"reason": "failed_on_chain"},
            )
            evidence.record(
                "failed_on_chain",
                live_trade_id=live_trade_id,
                ledger_status="open (unchanged — the position was not sold)",
                err=confirmation["err"],
            )
            _print_block(
                "TRANSACTION FAILED ON CHAIN — position NOT closed, fee paid",
                [
                    f"  signature   : {signed.signature}",
                    f"  on-chain err: {confirmation['err']}",
                    f"  slot        : {confirmation['slot']}",
                    "  The swap did not execute. The transaction fee was still",
                    f"  charged. live_trades #{live_trade_id} is STILL OPEN and",
                    "  the USDC is still in the wallet.",
                    f"  evidence    : {evidence.path}",
                ],
            )
            return EXIT_REFUSED

        if confirmation["outcome"] == "confirm_timeout":
            evidence.record(
                "confirmation_timeout",
                note="signature not seen inside the confirmation window — "
                "resolving rather than assuming",
            )
            code, resolution = await self._handle_reverse_ambiguity(
                evidence=evidence,
                decision_id=decision_id,
                live_trade_id=live_trade_id,
                signed=signed,
                build=build,
                signer_pubkey=signer_pubkey,
                mode=mode,
                bundle_id=bundle_id,
            )
            if code is not None:
                return code
            confirmation["outcome"] = "landed_after_resolution"
            confirmation["slot"] = resolution.slot
            confirmation["confirmation_status"] = resolution.confirmation_status
            # The state machine has to catch up with what the resolver found
            # before reconciliation can advance it to `reconciled`: the run is
            # still at `submission_attempted`, from which `reconciled` is not
            # reachable — and it must not be, because that would let a run
            # reconcile without ever recording that its transaction landed.
            await self._reverse_state(
                decision_id,
                (
                    STATE_FINALIZED
                    if resolution.confirmation_status == "finalized"
                    else STATE_LANDED
                ),
                mode=mode,
            )

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

        summary = await self._reconcile_reverse(
            evidence=evidence,
            decision_id=decision_id,
            live_trade_id=live_trade_id,
            selection=selection,
            signature=signed.signature,
            quote=quote,
            report=report,
            intent=intent,
            signer_pubkey=signer_pubkey,
            confirmation=confirmation,
            transaction=transaction,
            mode=mode,
        )
        return summary["exit_code"]

    async def _handle_reverse_ambiguity(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int,
        signed: SignedTransaction,
        build: SwapBuild,
        signer_pubkey: str,
        mode: str,
        bundle_id: str | None = None,
    ) -> tuple[int | None, ResolutionReport]:
        """Turn an ambiguous reverse submission into a decided state.

        Returns ``(exit_code, resolution)`` — or ``(None, resolution)`` when
        the transaction turns out to have landed and the caller continues into
        reconciliation.

        NO branch here rebuilds, resubmits, or CLOSES the row. A rebuild is a
        second signature and both can land: two sales where one was
        authorized. Closing on anything short of proven finality would assert a
        position was disposed of on the strength of not knowing.
        """
        pooled = await self._resolve(
            signature=signed.signature,
            last_valid_block_height=build.last_valid_block_height,
            owner_pubkey=signer_pubkey,
        )
        resolution = pooled.report
        evidence.record(
            "ambiguity_resolution",
            **_resolution_summary(resolution),
            **pooled.as_evidence(),
        )
        await self._record_bundle_diagnostics(evidence, bundle_id)

        if resolution.verdict == "landed":
            evidence.record(
                "ambiguity_adopted",
                signature=signed.signature,
                slot=resolution.slot,
                confirmation_status=resolution.confirmation_status,
                note="the transaction DID land — adopting it rather than " "rebuilding",
            )
            return None, resolution

        if resolution.verdict in ("failed_on_chain", "definitively_not_submitted"):
            ended = await self._terminalize_execution(
                decision_id, reason=f"reverse:{resolution.verdict}"
            )
            evidence.record(
                f"ambiguity_{resolution.verdict}",
                live_trade_id=live_trade_id,
                ledger_status="open (unchanged — the position was not sold)",
                execution_state=ended,
                on_chain_err=resolution.on_chain_err,
            )
            _print_block(
                "REVERSE SWAP DID NOT EXECUTE — position still open",
                [
                    f"  signature   : {signed.signature}",
                    f"  verdict     : {resolution.verdict}",
                    f"  on-chain err: {resolution.on_chain_err}",
                    f"  live_trades #{live_trade_id} is unchanged and STILL OPEN.",
                    "  The USDC is still in the wallet."
                    + (
                        "  A fee was paid."
                        if resolution.verdict == "failed_on_chain"
                        else ""
                    ),
                    "",
                    "  Rerunning builds a NEW transaction with a new signature",
                    "  and asks for a new authorization.",
                    f"  evidence    : {evidence.path}",
                ],
            )
            return EXIT_REFUSED, resolution

        # unresolved — the dangerous one. STOP. Never rebuild, never close.
        await update_live_trade(self._db, live_trade_id, status="needs_manual_review")
        await self._reverse_state(
            decision_id,
            STATE_SUBMISSION_UNKNOWN,
            mode=mode,
            detail={"reason": "resolver could not decide"},
        )
        evidence.record(
            "ambiguity_unresolved",
            live_trade_id=live_trade_id,
            ledger_status="needs_manual_review",
            note="a transaction may exist that sold this position; the row is "
            "marked for a human rather than closed or left reading as clean",
        )
        _print_block(
            "ESCALATE — REVERSE SUBMISSION UNRESOLVED, DO NOT REBUILD",
            [
                "  The cluster could not tell us whether this transaction",
                "  exists. It may still be in flight, and it may have sold the",
                "  position.",
                f"  signature       : {signed.signature}",
                f"  decision ID     : {decision_id}",
                f"  live_trades row : {live_trade_id} (needs_manual_review)",
                "",
                "  *** DO NOT REBUILD AND DO NOT RESUBMIT. ***",
                "  A rebuild produces a different blockhash and a different",
                "  signature, and BOTH can land — two sales where one was",
                "  authorized.",
                "",
                "  Next steps, in order:",
                "   1. Re-run the resolver as the blockhash expires:",
                "        python -m scout.live.solana_lane reverse-resolve "
                f"--decision-id {decision_id}",
                "   2. definitively_not_submitted -> the position was never",
                "      sold; the row goes back to open by hand.",
                "   3. landed -> the USDC was sold; the row closes only once",
                "      finality and the balances agree.",
                f"   4. Evidence: {evidence.path}",
            ],
        )
        return EXIT_ESCALATE, resolution

    async def _reconcile_reverse(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        live_trade_id: int,
        selection: dict[str, Any],
        signature: str,
        quote: SolanaQuote,
        report: VerificationReport,
        intent: dict[str, Any],
        signer_pubkey: str,
        confirmation: dict[str, Any],
        transaction: dict[str, Any] | None,
        mode: str,
    ) -> dict[str, Any]:
        """Compare the chain, the balances and the row — then close, or not.

        The two legs are asymmetric, and stating each as tightly as the facts
        allow is what makes "exact agreement" mean something:

        **USDC is EXACT.** The swap spends precisely the input amount; a token
        transfer has no fee and no slippage on the amount debited. Anything
        other than exactly ``amount_raw`` leaving is unexplained, full stop.

        **SOL is a BAND, and the band's width is the slippage the operator
        already approved.** The floor is the on-chain minimum output less every
        lamport that unconditionally leaves (base fee + priority fee + tip) and
        less any account rent that was spent and not returned. The ceiling
        allows the fill to land as far ABOVE the quote as the approved slippage
        allowed it to land below — a fill outside that in either direction is
        not the trade that was authorized.

        Rent is handled by counting rather than by assumption: the inspector
        reports which accounts are created and which are closed, so a route
        that opens and closes a wrapped-SOL account nets to zero and a route
        that opens one and keeps it costs the rent, without either shape being
        assumed.
        """
        sol_after = None
        usdc_after = None
        mismatches: list[str] = []
        try:
            sol_after = await self._rpc.get_balance(signer_pubkey)
        except Exception as exc:
            mismatches.append(f"get_balance: {type(exc).__name__}: {exc}")
        try:
            usdc_after = await self._rpc.get_token_balance(signer_pubkey, USDC_MINT)
        except Exception as exc:
            mismatches.append(f"get_token_balance: {type(exc).__name__}: {exc}")

        sol_before = int(intent["pre_submit_sol_lamports"])
        usdc_before = int(intent["pre_submit_usdc_raw"])
        amount_raw = int(intent["amount_raw"])
        rent_spent = int(report.ata_rent_lamports)
        rent_recovered = int(report.recovered_rent_lamports)
        unconditional = report.total_fee_lamports - rent_spent
        slack = LAMPORTS_PER_SIGNATURE

        usdc_spent = None if usdc_after is None else usdc_before - usdc_after
        sol_received = None if sol_after is None else sol_after - sol_before

        if usdc_spent is None:
            mismatches.append("no post-trade USDC balance to compare against")
        elif usdc_spent != amount_raw:
            mismatches.append(
                f"USDC spent {usdc_spent} raw is not the {amount_raw} raw this "
                "swap was authorized to spend — a token debit is exact, so any "
                "difference is a movement this run cannot account for"
            )

        # *** THIS COMPARISON IS THE SLIPPAGE GUARANTEE. ***
        # `tx_inspector` cannot read Jupiter's encoded otherAmountThreshold, so
        # what protects the trade is the on-chain program enforcing it and this
        # check confirming afterwards that it did.
        meets_minimum_output: bool | None = None
        net_rent = rent_spent - rent_recovered
        low = quote.min_out_amount - unconditional - net_rent - slack
        upside = quote.out_amount - quote.min_out_amount
        high = quote.out_amount + upside - unconditional - net_rent + slack
        implied_gross_out = None
        if sol_received is None:
            mismatches.append("no post-trade SOL balance to compare against")
        else:
            implied_gross_out = sol_received + unconditional + net_rent
            meets_minimum_output = implied_gross_out >= quote.min_out_amount
            if not meets_minimum_output:
                mismatches.append(
                    f"implied gross output {implied_gross_out} lamports is below "
                    f"the on-chain minimum {quote.min_out_amount} "
                    "(otherAmountThreshold) — the slippage bound did not hold"
                )
            if not (low <= sol_received <= high):
                mismatches.append(
                    f"SOL received {sol_received} lamports is outside the "
                    f"explained band [{low}, {high}] (minimum out "
                    f"{quote.min_out_amount} / quoted {quote.out_amount}, less "
                    f"{unconditional} of unconditional cost and {net_rent} of "
                    f"net account rent, ±{slack})"
                )

        _meta = (transaction or {}).get("meta") or {}
        on_chain_err = _meta.get("err")
        if on_chain_err is not None:
            mismatches.append(f"the finalized transaction carries err={on_chain_err}")
        if confirmation["outcome"] == "finalize_timeout":
            mismatches.append(
                "the transaction confirmed but was not finalized inside the "
                "window — it is landed but not yet rooted"
            )

        fresh = await fetch_live_trade_row(self._db, live_trade_id)
        if fresh is None:
            mismatches.append("the ledger row could not be re-read after the swap")
        elif fresh["status"] != "open":
            mismatches.append(
                f"live_trades #{live_trade_id} is {fresh['status']!r} rather "
                "than 'open' — something else moved it while this swap ran"
            )

        finalized = _is_finalized(confirmation)
        if not finalized:
            mismatches.append(
                f"the transaction is {confirmation['outcome']} rather than "
                "finalized; a position is closed only against a rooted "
                "transaction"
            )

        verdict = "pass" if not mismatches else "review"
        summary: dict[str, Any] = {
            "verdict": verdict,
            "mismatches": mismatches,
            "finalized": finalized,
            "chain": {
                "signature": signature,
                "slot": confirmation["slot"],
                "confirmation_status": confirmation["confirmation_status"],
                "fee_lamports": _meta.get("fee"),
                "on_chain_err": on_chain_err,
                "transaction_found": transaction is not None,
            },
            "balance": {
                "sol_before_lamports": sol_before,
                "sol_after_lamports": sol_after,
                "sol_received_lamports": sol_received,
                "sol_received": _fmt(sol_from_lamports(sol_received)),
                "sol_received_explained_band": [low, high],
                "implied_gross_out_lamports": implied_gross_out,
                "usdc_before_raw": usdc_before,
                "usdc_after_raw": usdc_after,
                "usdc_spent_raw": usdc_spent,
                "usdc_spent": _fmt(usdc_from_raw(usdc_spent)),
                "sol_minimum_lamports": quote.min_out_amount,
                "sol_quoted_lamports": quote.out_amount,
                "unconditional_cost_lamports": unconditional,
                "net_account_rent_lamports": net_rent,
                "meets_minimum_output": meets_minimum_output,
            },
        }

        # ---- close the row, or explain why not ----
        closed = False
        if verdict == "pass" and finalized:
            closed = await self._close_reverse_row(
                evidence=evidence,
                live_trade_id=live_trade_id,
                selection=selection,
                signature=signature,
                usdc_spent_raw=int(usdc_spent or 0),
                gross_out_lamports=int(implied_gross_out or 0),
                unconditional_cost_lamports=unconditional,
            )
            await self._reverse_state(
                decision_id,
                STATE_RECONCILED,
                mode=mode,
                detail={"reconciliation": "pass", "row_closed": closed},
            )
        summary["row_closed"] = closed
        evidence.record("reconciliation", **summary)
        if mismatches:
            log.warning("solana_reverse_reconciliation_review", mismatches=mismatches)

        lines = [
            f"  decision ID  : {decision_id}",
            f"  signature    : {signature}",
            f"  status       : {confirmation['outcome']} "
            f"({confirmation['confirmation_status']})",
            f"  slot         : {confirmation['slot']}",
            f"  USDC spent   : {summary['balance']['usdc_spent']} USDC",
            f"  SOL received : {summary['balance']['sol_received']} SOL (net of "
            f"{unconditional} lamports of fees)",
            f"  reconcile    : {verdict}",
            f"  ledger row   : {live_trade_id} "
            + (
                f"CLOSED ({_REVERSE_CLOSED_STATUS})"
                if closed
                else "left OPEN — see below"
            ),
            f"  evidence     : {evidence.path}",
        ]
        if not closed:
            lines += (
                [
                    "",
                    "  THE ROW WAS NOT CLOSED. A position is closed only against a",
                    "  finalized transaction whose balances agree exactly:",
                ]
                + [f"   - {m}" for m in mismatches]
                + [
                    "  Check the wallet before running the lane again.",
                ]
            )
        _print_block(f"SOLANA REVERSE SWAP {confirmation['outcome'].upper()}", lines)
        summary["exit_code"] = EXIT_OK if closed else EXIT_REVIEW
        return summary

    async def _close_reverse_row(
        self,
        *,
        evidence: EvidenceLog,
        live_trade_id: int,
        selection: dict[str, Any],
        signature: str,
        usdc_spent_raw: int,
        gross_out_lamports: int,
        unconditional_cost_lamports: int,
    ) -> bool:
        """Write the close. Called ONLY on finality plus exact agreement.

        The round trip is denominated in SOL, because SOL is what the lane
        actually put at risk: it bought USDC with SOL and has now bought SOL
        back. Denominating in USD would report ~0 by construction — the
        position WAS dollars — and would hide the only number that matters.

        Two different SOL figures, and conflating them would misreport both:

        ``exit_fill_price`` is priced off the GROSS output, because a price is
        what the swap executed at and fees are not part of it. Same USDC/SOL
        convention as ``mid_at_entry`` and ``entry_fill_price``.

        ``realized_pnl_*`` is computed off the NET output — gross less the base
        fee, priority fee and Jito tip — because P&L is what the wallet
        actually ended up holding. Account rent is deliberately excluded from
        that netting: rent is a refundable deposit rather than a cost, and
        counting a recovered deposit as profit would flatter the number.

        *** THE ENTRY LEG'S FEE IS NOT NETTED. *** ``live_trades`` records the
        entry FILL but not what the entry transaction cost, so this figure is
        optimistic by roughly one Solana transaction's fees (order 1e-4 SOL at
        the pilot's tip). Stated here rather than silently absorbed, because a
        P&L column that quietly means something different from the same column
        at another venue is worse than one that is documented.
        """
        sol_back = sol_from_lamports(gross_out_lamports) or Decimal("0")
        sol_net = sol_from_lamports(
            gross_out_lamports - unconditional_cost_lamports
        ) or Decimal("0")
        usdc_out = usdc_from_raw(usdc_spent_raw) or Decimal("0")
        entry_qty: Decimal = selection["entry_qty"]
        exit_price = (usdc_out / sol_back) if sol_back else None
        pnl_sol = sol_net - entry_qty
        pnl_usd = (pnl_sol * exit_price) if exit_price is not None else None
        pnl_pct = (pnl_sol / entry_qty * 100) if entry_qty else None
        await update_live_trade(
            self._db,
            live_trade_id,
            status=_REVERSE_CLOSED_STATUS,
            exit_order_id=signature,
            exit_fill_price=_fmt(exit_price),
            realized_pnl_usd=_fmt(pnl_usd),
            realized_pnl_pct=_fmt(pnl_pct),
            closed_at=datetime.now(timezone.utc).isoformat(),
        )
        evidence.record(
            "row_closed",
            live_trade_id=live_trade_id,
            status=_REVERSE_CLOSED_STATUS,
            exit_order_id=signature,
            entry_sol=_fmt(entry_qty),
            exit_sol_gross=_fmt(sol_back),
            exit_sol_net=_fmt(sol_net),
            unconditional_cost_lamports=unconditional_cost_lamports,
            usdc_spent=_fmt(usdc_out),
            exit_fill_price=_fmt(exit_price),
            realized_pnl_sol=_fmt(pnl_sol),
            realized_pnl_usd=_fmt(pnl_usd),
            realized_pnl_pct=_fmt(pnl_pct),
            note="closed against a FINALIZED transaction whose USDC debit was "
            "exact and whose SOL credit fell inside the authorized band",
        )
        log.info(
            "solana_reverse_row_closed",
            live_trade_id=live_trade_id,
            signature=signature,
            realized_pnl_sol=_fmt(pnl_sol),
        )
        return True

    # ---------------- reverse: read-only status ----------------
    async def reverse_status(self) -> int:
        """Read-only report on every reverse execution. Writes NOTHING.

        Deliberately not gated on SOLANA_MODE or the kill switch, and for the
        same reason ``status`` is not: turning the lane off is an operator's
        first instinct when something looks wrong, and it must not also turn
        off the thing that says what is wrong.

        Every non-terminal execution is put to the cluster BY ITS EXPECTED
        SIGNATURE and the verdict reported. Nothing is written on the strength
        of it — ``reverse-resolve`` is where a verdict is acted on — so this is
        safe to run at any point, including with a transaction in flight.
        """
        executions = await fetch_reverse_executions(self._db)
        lines: list[str] = [
            f"  SOLANA_MODE              : {self._settings.SOLANA_MODE}",
            f"  USDC account closure     : "
            f"{'ENABLED in config' if self._settings.SOLANA_REVERSE_CLOSE_USDC_ATA else 'DISABLED (default)'}",
            f"  database                 : {Path(self._settings.DB_PATH).resolve()}",
            f"  reverse executions open  : {len(executions)}",
        ]
        blockers = 0
        for execution in executions:
            detail = (
                execution["detail"] if isinstance(execution["detail"], dict) else {}
            )
            disposition = recovery_disposition(execution["state"])
            signature = execution.get("expected_signature")
            lines += [
                "",
                f"    decision id   : {execution['decision_id']}",
                f"    state         : {execution['state']}  ({disposition})",
                f"    target row    : live_trades #{detail.get('live_trade_id')}",
                f"    signature     : {signature or '(none — never signed)'}",
                f"    approval      : {detail.get('approval_digest', '(not recorded)')}",
                f"    pre-submit    : SOL {detail.get('pre_submit_sol_lamports')} "
                f"lamports / USDC {detail.get('pre_submit_usdc_raw')} raw",
            ]
            if disposition == "discard":
                lines.append(
                    "    verdict       : nothing was ever sent — the position "
                    "is untouched"
                )
                continue
            blockers += 1
            if not signature:
                lines.append(
                    "    verdict       : *** state says a transaction may exist "
                    "but no signature was recorded — escalate ***"
                )
                continue
            pooled = await self._resolve(
                signature=str(signature),
                last_valid_block_height=detail.get("last_valid_block_height"),
            )
            lane = apply_age_policy(
                pooled.report,
                age_seconds=row_age_seconds(execution.get("created_at")),
                settings=self._settings,
                had_evidence_height=detail.get("last_valid_block_height") is not None,
                pool_downgrade=pooled.downgrade_reason,
            )
            lines += [
                f"    cluster says  : {lane.verdict} "
                f"(slot {pooled.report.slot}, "
                f"{pooled.report.confirmation_status})",
                f"    would do      : {_reverse_resolve_guidance(lane.verdict)}",
            ]

        row_lines: list[str] = []
        for row in await fetch_blocking_rows(self._db):
            row_lines.append(
                f"    #{row['live_trade_id']} {row['status']} "
                f"size={row['size_usd']} entry_sig={row['entry_order_id']}"
            )
        lines += ["", f"  blocking ledger rows     : {len(row_lines)}"] + row_lines
        _print_block("SOLANA REVERSE SWAP STATUS (read-only)", lines)
        return EXIT_BLOCKED if blockers else EXIT_OK

    # ---------------- reverse: recovery ----------------
    async def reverse_resolve(self, *, decision_id: str) -> int:
        """Settle ONE interrupted reverse run, by its expected signature.

        The recovery path for all three places a run can die:

        ``after the intent was persisted, before submission``
            The state is pre-submission, so nothing was sent — provable from
            the record alone, without asking anyone. The run is retired and
            the position is left open.
        ``after submission``
            A transaction may exist. Its signature is in
            ``solana_executions``, so the cluster is asked about that one
            signature. Never a rebuild, never a resubmission.
        ``after finality, before the row was closed``
            The transaction landed and the row is still open. The balances
            recorded before submission are in the evidence file, so the same
            reconciliation the run would have done is done here — and the row
            closes only if it agrees exactly.
        """
        evidence = self._evidence_for(decision_id)
        evidence.record(
            "run_started", command="reverse-resolve", decision_id=decision_id
        )
        try:
            execution = await load_execution(self._db, decision_id)
            if execution is None:
                raise LaneAbort(
                    "lookup",
                    f"no solana_executions row with decision_id={decision_id}",
                )
            detail = (
                execution["detail"] if isinstance(execution["detail"], dict) else {}
            )
            if detail.get("direction") != DIRECTION_USDC_TO_SOL:
                raise LaneAbort(
                    "lookup",
                    f"execution {decision_id} is a "
                    f"{detail.get('direction') or 'forward'} run, not a reverse "
                    "one. Use `resolve` for a forward placement — the two "
                    "dispose of their ledger rows in opposite directions.",
                )
            evidence.record("execution_row", state=execution["state"], **detail)

            live_trade_id = detail.get("live_trade_id")
            state = execution["state"]
            if state in TERMINAL_STATES:
                _print_block(
                    "NOTHING TO RESOLVE",
                    [
                        f"  decision ID : {decision_id}",
                        f"  state       : {state} (terminal)",
                        f"  evidence    : {evidence.path}",
                    ],
                )
                return EXIT_OK

            if recovery_disposition(state) == "discard":
                ended = await self._terminalize_execution(
                    decision_id, reason="interrupted before submission"
                )
                evidence.record(
                    "discarded",
                    execution_state=ended,
                    live_trade_id=live_trade_id,
                    note="the state proves nothing was sent, so the position "
                    "is untouched and the lane is clear",
                )
                _print_block(
                    "NOTHING WAS EVER SENT — position untouched",
                    [
                        f"  decision ID     : {decision_id}",
                        f"  state           : {state} -> {ended}",
                        f"  live_trades row : {live_trade_id} (still open)",
                        "  The run died before submission. Rerunning `reverse`",
                        "  is safe: it builds a NEW transaction and asks for a",
                        "  new authorization.",
                        f"  evidence        : {evidence.path}",
                    ],
                )
                return EXIT_OK

            signature = execution.get("expected_signature")
            if not signature:
                raise LaneAbort(
                    "lookup",
                    f"execution {decision_id} is in state {state} — which means "
                    "a transaction may exist — but carries no expected "
                    "signature, so there is nothing to ask the cluster about. "
                    "This must be looked at by hand.",
                    exit_code=EXIT_ESCALATE,
                )

            _resolver_url, pinned = resolver_endpoint(self._settings)
            pool_health = await self._pool.check_health()
            evidence.record(
                "resolver_pool",
                endpoints=[entry.as_evidence() for entry in pool_health],
                usable_count=sum(1 for entry in pool_health if entry.usable),
                configured_count=len(pool_health),
                expected_genesis=SOLANA_MAINNET_GENESIS_HASH,
            )
            pooled = await self._resolve(
                signature=str(signature),
                last_valid_block_height=detail.get("last_valid_block_height"),
            )
            lane = apply_age_policy(
                pooled.report,
                age_seconds=row_age_seconds(execution.get("created_at")),
                settings=self._settings,
                had_evidence_height=detail.get("last_valid_block_height") is not None,
                pool_downgrade=pooled.downgrade_reason,
            )
            evidence.record(
                "resolution",
                **_resolution_summary(pooled.report),
                **pooled.as_evidence(),
                lane_verdict=lane.verdict,
                expiry_source=lane.expiry_source,
                age_policy_reason=lane.reason,
                resolver_endpoint_pinned=pinned,
            )
            return await self._apply_reverse_verdict(
                evidence=evidence,
                decision_id=decision_id,
                execution=execution,
                detail=detail,
                lane=lane,
                pooled=pooled,
                pinned=pinned,
                signature=str(signature),
            )
        except LaneAbort as abort:
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
                "solana_reverse_resolve_error",
                decision_id=decision_id,
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
            evidence.record(
                "unexpected_error", error_type=type(exc).__name__, error=str(exc)
            )
            print(f"ESCALATE [reverse-resolve]: {type(exc).__name__}: {exc}")
            return EXIT_ESCALATE

    async def _apply_reverse_verdict(
        self,
        *,
        evidence: EvidenceLog,
        decision_id: str,
        execution: dict[str, Any],
        detail: dict[str, Any],
        lane: LaneVerdict,
        pooled: PoolResolution,
        pinned: bool,
        signature: str,
    ) -> int:
        """What a resolved verdict licenses, and nothing more."""
        live_trade_id = detail.get("live_trade_id")
        row = (
            None
            if live_trade_id is None
            else await fetch_live_trade_row(self._db, int(live_trade_id))
        )
        lines = [
            f"  decision ID : {decision_id}",
            f"  signature   : {signature}",
            f"  target row  : live_trades #{live_trade_id} "
            f"({None if row is None else row['status']})",
            f"  verdict     : {lane.verdict}",
            f"  expiry from : {lane.expiry_source or 'not established'}",
            f"  resolver    : {pooled.endpoint}"
            + ("" if pinned else "   ** NOT PINNED - verdict is advisory **"),
            f"  corroborated: {_corroboration_line(pooled)}",
        ]

        if lane.verdict in ("definitively_not_submitted", "failed_on_chain"):
            if lane.verdict == "definitively_not_submitted" and not pinned:
                evidence.record(
                    "auto_action_withheld",
                    note="an absence read from a load-balanced endpoint can be "
                    "manufactured; neither row is advanced on it",
                )
                lines += [
                    "",
                    "  NOTHING WAS CHANGED. This verdict came from a",
                    "  load-balanced endpoint, where one node ahead on block",
                    "  height and missing the signature is enough to invent it.",
                    "  Point SOLANA_RESOLVER_RPC_URL at a dedicated node and",
                    "  run this again before acting.",
                ]
                _print_block("SOLANA REVERSE RESOLUTION", lines)
                return EXIT_BLOCKED
            ended = await self._terminalize_execution(
                decision_id, reason=f"reverse-resolve:{lane.verdict}"
            )
            # The position was NOT sold. If an earlier ambiguity marked the row
            # for review, that marking is what this verdict retires — the row
            # goes back to being an open position.
            restored = False
            if row is not None and row["status"] == "needs_manual_review":
                await update_live_trade(self._db, int(live_trade_id), status="open")
                restored = True
            evidence.record(
                "position_retained",
                live_trade_id=live_trade_id,
                execution_state=ended,
                verdict=lane.verdict,
                row_restored_to_open=restored,
                note="the reverse swap did not execute, so the position the row "
                "describes still exists",
            )
            lines += [
                "",
                "  The reverse swap did NOT execute"
                + (" — a fee was paid." if lane.verdict == "failed_on_chain" else "."),
                f"  live_trades #{live_trade_id} still holds the position"
                + (" (restored to 'open')." if restored else "."),
                "  Rerunning `reverse` is safe: it builds a NEW transaction and",
                "  asks for a new authorization.",
            ]
            _print_block("SOLANA REVERSE RESOLUTION", lines)
            return EXIT_OK

        if lane.verdict == "landed":
            finalized = pooled.report.confirmation_status == "finalized"
            state = await self._terminalize_execution(
                decision_id,
                reason="reverse-resolve:landed",
                target=STATE_FINALIZED if finalized else STATE_LANDED,
            )
            evidence.record(
                "landed",
                live_trade_id=live_trade_id,
                execution_state=state,
                confirmation_status=pooled.report.confirmation_status,
                slot=pooled.report.slot,
            )
            lines += [
                "",
                "  The reverse swap EXECUTED. The USDC was sold.",
                f"  execution   : {state}",
                "",
                "  This command does NOT close the ledger row. Closing a",
                "  position requires the pre-submission balances to compare",
                "  against, and reconciling them is a judgement about money",
                "  that belongs to a human once a run has already been",
                "  interrupted. The record it needs is in the evidence file:",
                f"    {evidence_path_for(self._settings.SOLANA_PILOT_EVIDENCE_DIR, decision_id)}",
                f"  Close live_trades #{live_trade_id} by hand once the wallet",
                "  agrees with it.",
            ]
            _print_block("SOLANA REVERSE RESOLUTION", lines)
            return EXIT_BLOCKED

        lines += [
            "",
            "  UNRESOLVED — we could not tell what happened.",
            "  *** DO NOT REBUILD AND DO NOT RERUN `reverse`. ***",
            "  Run this again once the blockhash has had time to expire;",
            "  absence only becomes definitive after that.",
        ]
        _print_block("SOLANA REVERSE RESOLUTION", lines)
        return EXIT_ESCALATE

    # ---------------- watchdog ----------------
    async def watchdog(self, session: Any) -> int:
        """Alert on executions stuck in a non-terminal state. For cron.

        Deliberately NOT gated on SOLANA_MODE or the kill switch, and for the
        same reason ``resolve`` is not: turning the lane off is an operator's
        first instinct when something looks wrong, and it must not also turn
        off the thing that would have told them what is wrong.

        Exit 0 when nothing is stuck, ``EXIT_BLOCKED`` when something is — so a
        cron wrapper can tell the two apart without parsing stdout.
        """
        summary = await check_stuck_executions(self._db, session, self._settings)
        if not summary["enabled"]:
            print("SOLANA_EXECUTION_WATCHDOG_ENABLED is False — nothing checked.")
            return EXIT_OK
        stuck = summary["stuck"]
        if not stuck:
            print("solana lane: no stuck executions.")
            return EXIT_OK
        _print_block(
            f"SOLANA LANE: {len(stuck)} STUCK EXECUTION(S)",
            [
                f"  {e['decision_id']} {e['state']} "
                f"{e['age_seconds'] / 60:.0f}m (threshold "
                f"{e['threshold_seconds'] / 60:.0f}m)"
                + ("  ** A TRANSACTION MAY EXIST **" if e["blocks_the_lane"] else "")
                for e in stuck
            ]
            + [
                "",
                f"  operator alerts sent this run: {summary['alerted']}",
                "  For anything marked above: NEVER rebuild. Run",
                "    python -m scout.live.solana_lane resolve --decision-id <id>",
            ],
        )
        return EXIT_BLOCKED

    # ---------------- status ----------------
    async def status(self) -> int:
        """Read-only lane report. Writes no evidence file — nothing decided."""
        settings = self._settings
        lines: list[str] = [
            f"  SOLANA_MODE              : {settings.SOLANA_MODE}"
            + (
                ""
                if settings.SOLANA_MODE in EXECUTING_MODES
                else "   (no execution in this mode)"
            ),
            f"  bounded autonomy flag    : "
            f"{settings.SOLANA_BOUNDED_AUTONOMOUS_ENABLED}",
            f"  authorization            : "
            f"{authorization_policy_for(settings.SOLANA_MODE).method}",
            f"  pair                     : {PAIR} (mainnet, fixed)",
            f"  per-swap band            : {settings.SOLANA_PILOT_MIN_ORDER_USD} - "
            f"{settings.SOLANA_PILOT_MAX_ORDER_USD} USD on the USDC output",
            f"  daily notional cap       : "
            f"{settings.SOLANA_MAX_DAILY_NOTIONAL_USD} USD (authorized, UTC day)",
            f"  concurrency              : "
            f"{settings.SOLANA_MAX_OPEN_POSITIONS} open position(s), "
            f"{settings.SOLANA_MAX_CONCURRENT_EXECUTIONS} execution(s) in flight",
            f"  slippage ceiling         : {settings.SOLANA_PILOT_SLIPPAGE_BPS} bps",
            f"  jito tip requested       : "
            f"{settings.SOLANA_PILOT_JITO_TIP_LAMPORTS} lamports",
            f"  rpc (read-only)          : {redact_endpoint(settings.SOLANA_RPC_URL)}",
            f"  resolver endpoints       : {', '.join(self._pool.labels)}"
            + (
                ""
                if resolver_endpoint(settings)[1]
                else "   ** NOT PINNED - a real place will refuse "
                "(--simulate-only still runs) **"
            ),
            "  corroboration            : "
            + (
                f"available ({self._pool.size} endpoints)"
                if self._pool.size > 1
                else "UNAVAILABLE - one endpoint, so a definitive "
                "'not submitted' rests on a single node"
            ),
            f"  block engine (submit)    : {settings.JITO_BLOCK_ENGINE_URL}",
            f"  database                 : {Path(settings.DB_PATH).resolve()}",
        ]

        # Probed rather than assumed. Saying "corroboration: available" on the
        # strength of a config line would be the claim an operator most wants
        # checked BEFORE trade day — a second endpoint that is down, slow or on
        # another chain looks identical in config to one that works.
        for entry in await self._pool.check_health():
            lines.append(
                f"  endpoint {entry.label:<28}: "
                + (
                    ("ok" if not entry.degraded else "DEGRADED")
                    + f" ({entry.latency_ms}ms)"
                    if entry.usable
                    else f"UNUSABLE — {entry.detail}"
                )
            )

        kill_state = await self._ks.is_active()
        lines.append(
            "  kill switch              : "
            + (
                "INACTIVE"
                if kill_state is None
                else f"ACTIVE (#{kill_state.kill_event_id}, {kill_state.reason})"
            )
        )

        # `status` reads state; it must never hold signing capability. Custody
        # comes from stat, the wallet from config, and the key file is checked
        # only via its public half — no Keypair is constructed anywhere here.
        declared = (settings.SOLANA_PILOT_SIGNER_PUBKEY or "").strip()
        signer_pubkey: str | None = declared or None
        try:
            self._probe_custody()
        except SolanaKeypairError as exc:
            lines.append(f"  keypair custody          : REFUSED — {exc}")
        except Exception as exc:  # pragma: no cover - prober contract is typed
            lines.append(
                f"  keypair custody          : FAILED {type(exc).__name__}: {exc}"
            )
        else:
            lines.append("  keypair custody          : ok (mode and owner from stat)")

        lines.append(f"  declared signer          : {declared or '(none)'}")
        from_file, detail = keyfile_public_half(settings)
        if from_file is None:
            lines.append(
                f"  key file public half     : UNREADABLE — {detail} "
                "(no signer was constructed)"
            )
        elif from_file == declared:
            lines.append(
                "  key file public half     : matches the declared signer "
                "(read from the file; no signer constructed)"
            )
        else:
            lines.append(
                f"  key file public half     : MISMATCH — file holds {from_file}, "
                f"config declares {declared}. `place` will refuse at signing."
            )

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

        # The two axes, checked against each other. A stranded execution is
        # invisible from either table alone — the ledger says finished, the
        # execution says in flight — and it is what stops `place` while every
        # other line here reads clear.
        incoherent = await find_incoherent_executions(self._db)
        if incoherent:
            lines.append(
                f"  ** AXES DISAGREE         : {len(incoherent)} execution(s) "
                "still open against a finished ledger row — `place` WILL refuse"
            )
            for entry in incoherent:
                lines.append(
                    f"     {entry['decision_id']} execution="
                    f"{entry['execution_state']} ledger={entry['ledger_status']}"
                )
        else:
            lines.append("  lifecycle/money axes     : coherent")

        # Current exposure against the envelope, so "how much room is left
        # today" is answerable without running a placement to find out.
        exposure = await fetch_lane_exposure(self._db, self._settings)
        lines.append(
            f"  used today               : "
            f"{_fmt(exposure.notional_usd_today)} of "
            f"{settings.SOLANA_MAX_DAILY_NOTIONAL_USD} USD, "
            f"{exposure.open_positions} open position(s), "
            f"{exposure.active_executions} execution(s) in flight"
        )

        _print_block("SOLANA LANE STATUS", lines)
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
                "       python -m scout.live.solana_lane resolve --decision-id <id>",
                "     It retires the row automatically once the verdict becomes",
                "     definitively_not_submitted. NEVER rebuild in the meantime.",
            ]
        )
        _print_block("SOLANA LANE BLOCKED", lines)


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


def route_fingerprint(quote: SolanaQuote) -> str:
    """The FULL route, as a canonical string an authorization can bind to.

    ``_route_summary`` is for humans and collapses a route to its AMM labels;
    two different routes through the same two AMMs render identically there.
    This one carries every field of every step, in order, so any change to how
    the swap is actually executed changes the digest — which is the property
    the approval is supposed to have.

    An empty route plan renders as ``(none)`` rather than an empty string, so
    "no steps were reported" and "the field was omitted" cannot digest alike.
    """
    if not quote.route_plan:
        return "(none)"
    return ";".join(
        "|".join(
            (
                step.label or "(unlabelled)",
                step.amm_key,
                step.input_mint,
                step.output_mint,
                str(step.in_amount),
                str(step.out_amount),
            )
        )
        for step in quote.route_plan
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


def _entry_economics(
    *,
    amount_lamports: int,
    usdc_received: int | None,
    quoted_out_raw: int,
    fee_lamports: int | None,
) -> dict[str, Any]:
    """What the position cost to open, in the terms a later disposal needs.

    Not P&L on the trade — a SOL->USDC buy has no realised P&L until the USDC
    is disposed of, and writing an execution cost into ``realized_pnl_usd``
    would misreport it to every consumer that aggregates that column. What IS
    realised here is the cost of ENTRY: the executed price, the transaction
    fee, and how far the fill landed from the quote the operator authorized.

    ``entry_slippage_bps`` is positive when the fill was WORSE than quoted,
    matching the sign convention of the column it is written to.
    """
    sol_spent = _dec(amount_lamports) / _dec(LAMPORTS_PER_SOL)
    received = usdc_from_raw(usdc_received)
    quoted = usdc_from_raw(quoted_out_raw)
    executed_price = None if received is None or not sol_spent else received / sol_spent
    slippage_bps = None
    if received is not None and quoted:
        slippage_bps = int(((quoted - received) / quoted * 10_000).to_integral_value())
    return {
        "sol_spent": _fmt(sol_spent),
        "sol_spent_lamports": amount_lamports,
        "usdc_received": _fmt(received),
        "usdc_received_raw": usdc_received,
        "usdc_quoted": _fmt(quoted),
        "executed_price_usdc_per_sol": _fmt(executed_price),
        "quoted_price_usdc_per_sol": _fmt(
            None if not sol_spent or quoted is None else quoted / sol_spent
        ),
        "entry_slippage_bps": slippage_bps,
        "transaction_fee_lamports": fee_lamports,
        "transaction_fee_sol": _fmt(sol_from_lamports(fee_lamports)),
        "note": "entry economics, not realised P&L — the position is still open",
    }


def _is_finalized(confirmation: dict[str, Any]) -> bool:
    """Is this transaction ROOTED, as opposed to merely landed?

    Two spellings of the same fact, and both are required to be explicit:
    the confirmation poll reaching 'finalized' on its own, or the resolver
    having adopted a transaction the poll missed AND reporting it finalized.
    'landed', 'confirmed' and 'finalize_timeout' are all NOT this — a fork can
    still take those back, and a position must not be recorded as disposed of
    against something reversible.
    """
    outcome = confirmation.get("outcome")
    if outcome == "finalized":
        return True
    return (
        outcome == "landed_after_resolution"
        and confirmation.get("confirmation_status") == "finalized"
    )


def _reverse_resolve_guidance(verdict: str) -> str:
    """One line: what ``reverse-resolve`` would do with this verdict."""
    if verdict == "landed":
        return "the USDC was sold — the row closes only on finality + agreement"
    if verdict == "failed_on_chain":
        return "the swap failed; the position is retained and a fee was paid"
    if verdict == "definitively_not_submitted":
        return "nothing landed; the position is retained and the lane clears"
    return "cannot tell — the row keeps blocking; NEVER rebuild"


def _corroboration_line(pooled: PoolResolution) -> str:
    """One line saying whether a second endpoint backed a definitive absence.

    Spelled out rather than left to a boolean because "not corroborated" has
    three different meanings — nothing to corroborate, nobody to ask, and asked
    and disagreed — and only the last one is a problem.
    """
    corroboration = pooled.corroboration
    if pooled.raw_verdict != "definitively_not_submitted":
        return "n/a (only a definitive absence needs a second opinion)"
    if not corroboration.attempted:
        return "NO — single endpoint, verdict rests on one node"
    if corroboration.agrees:
        return f"yes — {corroboration.endpoint} saw the same absence"
    return f"DISAGREED — {corroboration.endpoint}: {corroboration.detail}"


def _verdict_guidance(
    verdict: str, retired: bool, *, lane_is_clear: bool = False
) -> list[str]:
    """What the operator should do next.

    ``lane_is_clear`` is the ONLY thing that licenses "rerunning is safe", and
    it is computed from what `place` will actually find — not from the verdict
    and not from whether the ledger row was retired. Those came apart in
    production: a definitive verdict retired the ledger row, printed "rerunning
    `place` is safe", and left the execution row blocking, so the very next
    `place` refused. A message about the lane being clear must be true of the
    command it is telling the operator to run.
    """
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
            "  exists. Record the cost."
            + (
                " The row is retired and the lane is clear."
                if lane_is_clear
                else " The row stays until it is retired by hand."
            ),
        ]
    if verdict == "definitively_not_submitted":
        lines = [
            "",
            "  The transaction can NEVER land: absent from the cluster and its",
            "  blockhash has expired.",
        ]
        if lane_is_clear:
            lines += [
                "  Both the ledger row and the execution row are closed, so the",
                "  lane is clear. Rerunning `place` is safe — it builds a new",
                "  transaction and asks for a new authorization.",
            ]
        elif retired:
            lines += [
                "  The ledger row is retired, but the EXECUTION row is still open,",
                "  so `place` will refuse at execution recovery. Do NOT rerun yet.",
            ]
        else:
            lines += [
                "  Nothing was changed — see the note above for why. `place` will",
                "  still refuse, so do NOT rerun yet.",
            ]
        return lines
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


def _usdc_arg(raw: str) -> Decimal:
    """argparse converter for ``--usdc``. Same four rejections as ``--sol``.

    The fractional-unit case bites HARDER here than on the SOL side: USDC has
    6 decimals, so ``--usdc 6.8996185`` is an ordinary thing to type (it is
    what a price calculation produces) and it is not expressible. Truncating it
    would swap a different amount than the one the operator asked for and than
    the one the approval screen showed.
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
        raw_from_usdc(value)
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
            f"{raw!r} is not a lane decision id (expected a dashed UUID, e.g. "
            "6d1b345e-2821-40e2-ad83-4ecb18a06876)"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solana_lane",
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
        help="force SIMULATION_ONLY for this run; can only NARROW SOLANA_MODE",
    )
    place.add_argument(
        "--yes-i-am-rehearsing",
        action="store_true",
        help="required with --simulate-only; refused without it",
    )

    reverse = sub.add_parser(
        "reverse",
        help="close ONE named Solana ledger row with a supervised USDC->SOL swap",
    )
    reverse.add_argument(
        "--live-trade-id",
        type=int,
        required=True,
        help="the live_trades row to close. Must be an OPEN solana row; a row "
        "at any other venue is refused.",
    )
    reverse.add_argument(
        "--usdc", type=_usdc_arg, required=True, help="exact USDC amount to swap"
    )
    reverse.add_argument(
        "--close-usdc-ata",
        action="store_true",
        help="ALSO permit the swap to close the wallet's USDC account and "
        "recover its rent. OFF by default and refused unless "
        "SOLANA_REVERSE_CLOSE_USDC_ATA is also True. Shown on its own line of "
        "the approval screen with the exact rent.",
    )
    reverse.add_argument(
        "--simulate-only",
        action="store_true",
        help="force SIMULATION_ONLY for this run; can only NARROW SOLANA_MODE",
    )
    reverse.add_argument(
        "--yes-i-am-rehearsing",
        action="store_true",
        help="required with --simulate-only; refused without it",
    )

    resolve = sub.add_parser(
        "resolve", help="ask the cluster what a persisted signature did"
    )
    resolve.add_argument("--decision-id", type=_decision_id_arg, required=True)

    reverse_resolve = sub.add_parser(
        "reverse-resolve",
        help="settle ONE interrupted reverse swap by its expected signature",
    )
    reverse_resolve.add_argument("--decision-id", type=_decision_id_arg, required=True)

    sub.add_parser(
        "reverse-status",
        help="read-only report on every outstanding reverse swap",
    )
    sub.add_parser("status", help="read-only lane report")
    sub.add_parser(
        "watchdog",
        help="alert on executions stuck in a non-terminal state (for cron)",
    )
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

    if args.command in ("place", "reverse"):
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
    # `.exists()` alone was NOT enough: a ZERO-BYTE file exists, so the old check
    # passed and the lane read "no kill switch, no prior signature to reconcile"
    # out of an empty file — the state this module's own docstring warns about.
    # `assert_safe_database` also requires a real SQLite header, the core tables
    # and recorded migrations, and resolves a relative DB_PATH against the
    # DEPLOYMENT ROOT instead of the process working directory.
    try:
        db_path = assert_safe_database(
            settings.DB_PATH, purpose="the Solana execution lane"
        )
    except UnsafeDatabase as exc:
        print(
            f"REFUSED [database:{exc.reason}]: {exc.message}\n"
            "  Proceeding would disable the kill switch and the startup "
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
    # `reverse` takes the SAME lock as `place`, not one of its own. The two are
    # different actions but they draw on one wallet and one ledger, and two
    # processes each holding "the other direction's" lock would submit two
    # transactions from the same key against each other's assumptions.
    if args.command in ("place", "reverse"):
        try:
            lock_fd, lock_path = acquire_lane_lock(db_path)
        except LaneLockHeld as held:
            print(
                f"REFUSED [lock]: another lane run holds {held.lock_path}\n"
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
            # The general read client plus a resolver POOL, all on one session.
            # The resolver's endpoints are separate from the general one
            # because a definitive verdict combines two facts that must come
            # from the same view of the chain, and a load-balanced general
            # endpoint is fine for quotes and simulation but not for that.
            # A one-URL pool is the normal single-node deployment.
            pool = ResolverPool.from_settings(settings, session)
            runner = LaneRunner(
                settings=settings,
                db=db,
                jupiter=JupiterClient(settings, session),
                rpc=SolanaRpcClient(settings, session),
                jito=JitoClient(settings, session),
                kill_switch=KillSwitch(db),
                resolver_rpc=pool.endpoints[0].client,
                resolver_pool=pool,
            )
            if args.command == "place":
                # --simulate-only NARROWS: it can turn an executing mode into a
                # rehearsal, never the reverse. Passing None lets the runner
                # take SOLANA_MODE as configured, so a flag can never escalate
                # a DISABLED lane into a live one.
                override = MODE_SIMULATION_ONLY if args.simulate_only else None
                return await runner.place(sol=args.sol, mode=override)
            if args.command == "reverse":
                override = MODE_SIMULATION_ONLY if args.simulate_only else None
                return await runner.reverse_place(
                    live_trade_id=args.live_trade_id,
                    usdc=args.usdc,
                    mode=override,
                    close_usdc_ata=args.close_usdc_ata,
                )
            if args.command == "resolve":
                return await runner.resolve(decision_id=args.decision_id)
            if args.command == "reverse-resolve":
                return await runner.reverse_resolve(decision_id=args.decision_id)
            if args.command == "reverse-status":
                return await runner.reverse_status()
            if args.command == "watchdog":
                return await runner.watchdog(session)
            return await runner.status()
        finally:
            await session.close()
            await db.close()
    finally:
        if lock_fd is not None and lock_path is not None:
            release_lane_lock(lock_fd, lock_path)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
