"""The operator-invoked supervised USDC->SOL reverse swap.

Same shape of assertion as ``test_live_solana_lane.py`` and for the same
reason: the load-bearing claims are NEGATIVE. No Jito POST when a gate
refuses, none when the row named is not a closeable Solana position, none when
the state moved between the approval screen and the signing step, and never a
second one after an ambiguous submission. A "refusal" that still submitted, or
a row closed against something short of a finalized and reconciled
transaction, is the failure this whole command exists to prevent.

Transactions are real ``solders`` v0 objects from ``tests/solana_tx_builder.py``
— ``build_reverse_swap_tx`` this time, whose instruction list is the reverse
direction's rather than the forward one's. Both SHAPES of the reverse route are
exercised: with the temporary wrapped-SOL account and without it, so neither
becomes accidentally load-bearing.

RPC ordering note: every Solana JSON-RPC call goes to one URL and
``aioresponses`` serves same-URL mocks in registration order, so the helpers
register payloads in the exact order ``reverse`` issues them. That order is
itself part of what these tests pin down — in particular that the balances are
re-read AFTER the approval prompt and not only before it.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import aiohttp
import pytest
from aioresponses import aioresponses
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from scout.config import Settings
from scout.db import Database
from scout.live import solana_lane
from scout.live.kill_switch import KillSwitch
from scout.live.solana.constants import (
    SOL_MINT,
    SOLANA_MAINNET_GENESIS_HASH,
    USDC_MINT,
)
from scout.live.solana.jito_client import JitoClient
from scout.live.solana.jupiter_client import JupiterClient
from scout.live.solana.limits import (
    DIRECTION_SOL_TO_USDC,
    DIRECTION_USDC_TO_SOL,
    OPERATOR_EXIT_AUTHORIZATION,
    LaneExposure,
    LimitsEngine,
    OperatorExitBinding,
)
from scout.live.solana.resolver_pool import ResolverPool
from scout.live.solana.rpc_client import SolanaRpcClient
from scout.live.solana.signer import SolanaKeypairError, sign_transaction
from scout.live.solana.tx_inspector import (
    derive_associated_token_address,
    verify_swap_transaction,
)
from scout.live.solana_lane import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    EXIT_BLOCKED,
    EXIT_ESCALATE,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_REVIEW,
    LaneRunner,
    build_parser,
    build_reverse_snapshot,
    reverse_authorization_token,
    reverse_intent_digest,
    route_fingerprint,
)
from solana_tx_builder import (
    PAYER,
    PAYER_PUBKEY,
    STRANGER,
    build_reverse_swap_tx,
    build_swap_tx,
    token_close_account_ix,
)

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
_PLANTED_KEYPAIR_PATH = "/srv/secrets/planted-solana-id-DO-NOT-LEAK.json"

_QUOTE_RE = __import__("re").compile(r"https://api\.jup\.ag/swap/v1/quote.*")
_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
_RPC_URL = "https://dedicated-node.example-rpc.net/rpc"
_SUBMIT_RE = __import__("re").compile(
    r"https://mainnet\.block-engine\.jito\.wtf/api/v1/transactions.*"
)
_TIP_ACCOUNTS_URL = "https://mainnet.block-engine.jito.wtf/api/v1/getTipAccounts"

# The real position this command exists for: 0.0985 SOL bought 6.899618 USDC.
# Rounded to numbers that divide cleanly so a reader can check the
# reconciliation arithmetic by hand.
_USDC_IN = Decimal("6.899618")
_USDC_IN_RAW = 6_899_618
_SOL_OUT = 94_500_000  # 0.0945 SOL
_SOL_MIN_OUT = 93_555_000  # 100 bps below

_ENTRY_QTY = "0.1"  # SOL that went in when the position was opened
_ENTRY_PRICE = "70.0"  # USDC per SOL at entry -> the row claims 7.0 USDC
_ENTRY_SIZE_USD = "7.0"

_LAST_VALID = 283_000_500
_HEIGHT_FRESH = 283_000_100

_ATA_RENT = 2_039_280
_BASE_FEE = 5_000
_PRIORITY_FEE = 200  # 1000 micro-lamports/CU x 200_000 CU
_TIP = 100_000
# One account created (the wrapped-SOL proceeds account) in the default shape.
_TOTAL_FEE = _BASE_FEE + _PRIORITY_FEE + _TIP + _ATA_RENT  # 2_144_480
# What unconditionally leaves the wallet: the created account's rent comes back
# when the same transaction closes it again.
_UNCONDITIONAL = _BASE_FEE + _PRIORITY_FEE + _TIP  # 105_200

_SOL_BEFORE = 500_000_000
_SOL_AFTER = _SOL_BEFORE + _SOL_OUT - _UNCONDITIONAL  # 594_394_800
_USDC_BEFORE = _USDC_IN_RAW
_USDC_AFTER = 0


# ----------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------
def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        SOLANA_MODE="SUPERVISED_LIVE",
        SOLANA_PILOT_KEYPAIR_PATH=_PLANTED_KEYPAIR_PATH,
        SOLANA_PILOT_SIGNER_PUBKEY=PAYER_PUBKEY,
        SOLANA_RPC_URL=_RPC_URL,
        SOLANA_PILOT_EVIDENCE_DIR=str(tmp_path / "evidence"),
        SOLANA_SUBMISSION_SETTLE_SEC=0.0,
        SOLANA_PILOT_POLL_INTERVAL_SEC=0.0,
        SOLANA_PILOT_CONFIRM_TIMEOUT_SEC=0.05,
        SOLANA_PILOT_FINALIZE_TIMEOUT_SEC=0.05,
        DB_PATH=str(tmp_path / "lane.db"),
    )
    base.update(overrides)
    return Settings(_env_file=None, **_REQUIRED, **base)


class _LoaderSpy:
    """Stands in for ``signer.load_keypair``, and COUNTS being called.

    The count is the point. "The funded key was never read before the operator
    authorized" is a claim about an absence, and the only way to assert an
    absence is to make the thing being absent observable.
    """

    def __init__(self, keypair):
        self._keypair = keypair
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self._keypair is None:
            raise SolanaKeypairError(
                f"keypair file {_PLANTED_KEYPAIR_PATH} could not be loaded"
            )
        return self._keypair


class _CustodyProbeSpy:
    def __init__(self, *, ok=True):
        self._ok = ok
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self._ok:
            raise SolanaKeypairError(
                f"keypair file {_PLANTED_KEYPAIR_PATH} has mode 0o644; "
                "required 0o600 (owner read/write only)"
            )
        return {
            "custody_verified": True,
            "signing_key_file": _PLANTED_KEYPAIR_PATH,
            "key_material_read": False,
        }


async def _make_runner(tmp_path, *, keypair=PAYER, custody_ok=True, **overrides):
    settings = _settings(tmp_path, **overrides)
    db = Database(tmp_path / "lane.db")
    await db.initialize()
    session = aiohttp.ClientSession()
    loader = _LoaderSpy(keypair)
    prober = _CustodyProbeSpy(ok=custody_ok)
    pool = ResolverPool.from_settings(settings, session)
    runner = LaneRunner(
        settings=settings,
        db=db,
        jupiter=JupiterClient(settings, session),
        rpc=SolanaRpcClient(settings, session),
        jito=JitoClient(settings, session),
        kill_switch=KillSwitch(db),
        keypair_loader=loader,
        custody_prober=prober,
        resolver_rpc=pool.endpoints[0].client,
        resolver_pool=pool,
    )
    runner.loader_spy = loader
    runner.custody_spy = prober
    return runner, db, session


def _authorize(monkeypatch, typed: str | None = None, *, on_prompt=None) -> None:
    """Feed the approval prompt. ``None`` raises EOF (non-interactive stdin).

    ``on_prompt`` runs AT the moment the prompt is displayed, which is how the
    ordering assertions observe what had and had not happened by then.
    """

    def _fake_input(_prompt: str = "") -> str:
        if on_prompt is not None:
            on_prompt()
        if typed is None:
            raise EOFError
        return typed

    monkeypatch.setattr("builtins.input", _fake_input)


def _message_sha256(tx_b64: str) -> str:
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    return hashlib.sha256(to_bytes_versioned(tx.message)).hexdigest()


def _expected_signature(tx_b64: str) -> str:
    return sign_transaction(tx_b64, PAYER, expected_signer=PAYER_PUBKEY).signature


def _quote_payload(**overrides) -> dict:
    payload = {
        "inputMint": USDC_MINT,
        "inAmount": str(_USDC_IN_RAW),
        "outputMint": SOL_MINT,
        "outAmount": str(_SOL_OUT),
        "otherAmountThreshold": str(_SOL_MIN_OUT),
        "swapMode": "ExactIn",
        "slippageBps": 100,
        "priceImpactPct": "0.0001",
        "contextSlot": 283_000_111,
        "routePlan": [
            {
                "swapInfo": {
                    "ammKey": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
                    "label": "Orca",
                    "inputMint": USDC_MINT,
                    "outputMint": SOL_MINT,
                    "inAmount": str(_USDC_IN_RAW),
                    "outAmount": str(_SOL_OUT),
                },
                "percent": 100,
            }
        ],
    }
    payload.update(overrides)
    return payload


def _rpc(value) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": value}


def _sig_status(
    *, known: bool = True, err=None, status: str = "finalized", slot: int = 283_000_455
) -> dict:
    value = (
        None
        if not known
        else {
            "slot": slot,
            "confirmations": None if status == "finalized" else 12,
            "err": err,
            "confirmationStatus": status,
        }
    )
    return _rpc({"context": {"slot": slot}, "value": [value]})


def _simulation_ok(err=None) -> dict:
    return _rpc(
        {
            "context": {"slot": 283_000_111},
            "value": {"err": err, "logs": ["Program log: ok"], "unitsConsumed": 91_000},
        }
    )


def _token_accounts(raw: int) -> dict:
    if raw == 0:
        return _rpc({"context": {"slot": 1}, "value": []})
    return _rpc(
        {
            "context": {"slot": 1},
            "value": [
                {
                    "account": {
                        "data": {
                            "parsed": {"info": {"tokenAmount": {"amount": str(raw)}}}
                        }
                    }
                }
            ],
        }
    )


def _mock_quote_and_build(m: aioresponses, tx_b64: str, **quote_kw) -> None:
    m.get(_QUOTE_RE, payload=_quote_payload(**quote_kw))
    m.post(
        _SWAP_URL,
        payload={
            "swapTransaction": tx_b64,
            "lastValidBlockHeight": _LAST_VALID,
            "prioritizationFeeLamports": 200,
        },
    )
    m.post(_TIP_ACCOUNTS_URL, payload=_rpc([_TIP_ACCOUNT]), repeat=True)


_TIP_ACCOUNT = "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"


def _mock_pre_approval_rpc(
    m: aioresponses,
    *,
    sol_before: int = _SOL_BEFORE,
    usdc_before: int = _USDC_BEFORE,
    simulation_err=None,
) -> None:
    """The RPC reads ``reverse`` issues up to the prompt, in order.

    resolver-pool genesis + health -> ATA rent -> SOL balance -> USDC balance
    -> simulate -> the display-time block height on the approval screen.
    """
    m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
    m.post(_RPC_URL, payload=_rpc("ok"))
    m.post(_RPC_URL, payload=_rpc(_ATA_RENT))
    m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": sol_before}))
    m.post(_RPC_URL, payload=_token_accounts(usdc_before))
    m.post(_RPC_URL, payload=_simulation_ok(simulation_err))
    m.post(_RPC_URL, payload=_rpc(_HEIGHT_FRESH))


def _mock_post_approval_rpc(
    m: aioresponses,
    *,
    height: int = _HEIGHT_FRESH,
    sol_at_signing: int = _SOL_BEFORE,
    usdc_at_signing: int = _USDC_BEFORE,
) -> None:
    """Blockhash re-check, then the pre-SIGNING balance re-read.

    Those last two calls are the ones that make "re-read immediately before
    signing" a fact rather than a claim: they exist only on this side of the
    approval prompt.
    """
    m.post(_RPC_URL, payload=_rpc(height))
    m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": sol_at_signing}))
    m.post(_RPC_URL, payload=_token_accounts(usdc_at_signing))


def _mock_settlement_rpc(
    m: aioresponses,
    *,
    status: dict | None = None,
    sol_after: int = _SOL_AFTER,
    usdc_after: int = _USDC_AFTER,
    tx_err=None,
) -> None:
    m.post(_RPC_URL, payload=status or _sig_status())
    m.post(
        _RPC_URL,
        payload=_rpc(
            {"slot": 283_000_455, "meta": {"fee": _UNCONDITIONAL, "err": tx_err}}
        ),
    )
    m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": sol_after}))
    m.post(_RPC_URL, payload=_token_accounts(usdc_after))


def _mock_happy_path(m: aioresponses, tx_b64: str, **settlement) -> None:
    _mock_quote_and_build(m, tx_b64)
    _mock_pre_approval_rpc(m)
    _mock_post_approval_rpc(m)
    m.post(
        _SUBMIT_RE,
        payload=_rpc(_expected_signature(tx_b64)),
        headers={"x-bundle-id": "bundle-rev"},
    )
    _mock_settlement_rpc(m, **settlement)


# ----------------------------------------------------------------------
# Ledger seeding
# ----------------------------------------------------------------------
async def _seed_position(
    db: Database,
    *,
    venue: str = "solana",
    status: str = "open",
    entry_fill_price: str | None = _ENTRY_PRICE,
    entry_fill_qty: str | None = _ENTRY_QTY,
    exit_order_id: str | None = None,
    size_usd: str = _ENTRY_SIZE_USD,
) -> int:
    """One reconciled forward position, as the ledger holds it after `place`."""
    anchor = await solana_lane.ensure_lane_anchor(db)
    async with db._txn_lock:
        cur = await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, mid_at_entry, entry_order_id, entry_fill_price,
                entry_fill_qty, exit_order_id, status, client_order_id,
                created_at)
               VALUES (?, 'solana-lane', 'SOL', ?, 'SOL/USDC', 'solana_lane',
                       ?, ?, 'entry-sig-forward', ?, ?, ?, ?, ?, ?)""",
            (
                anchor,
                venue,
                size_usd,
                _ENTRY_PRICE,
                entry_fill_price,
                entry_fill_qty,
                exit_order_id,
                status,
                f"forward-{venue}-{status}-{datetime.now(timezone.utc).timestamp()}",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db._conn.commit()
        return int(cur.lastrowid)


def _evidence_files(tmp_path):
    return sorted(
        (tmp_path / "evidence").glob("solana_lane_*.json"),
        key=lambda p: p.stat().st_mtime,
    )


def _steps(tmp_path, index: int = -1) -> list[dict]:
    raw = _evidence_files(tmp_path)[index].read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _step(steps: list[dict], name: str) -> dict | None:
    return next((s for s in steps if s["step"] == name), None)


def _submissions(mocked: aioresponses) -> list:
    out = []
    for key, calls in mocked.requests.items():
        if _SUBMIT_RE.search(str(key[1])):
            out.extend(calls)
    return out


async def _row(db: Database, live_trade_id: int) -> dict | None:
    return await solana_lane.fetch_live_trade_row(db, live_trade_id)


# ======================================================================
# Row selection — the one mistake that cannot be undone later
# ======================================================================
async def test_reverse_refuses_a_kraken_row(tmp_path):
    """Naming another venue's position must never reach a quote."""
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db, venue="kraken")
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "row_selection"
    assert "'kraken'" in aborted["reason"]
    assert _step(_steps(tmp_path), "quote") is None
    assert (await _row(db, row_id))["status"] == "open"  # untouched
    await session.close()
    await db.close()


@pytest.mark.parametrize(
    "status", ["rejected", "needs_manual_review", "closed_via_reconciliation"]
)
async def test_reverse_refuses_a_row_that_is_not_open(tmp_path, status):
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db, status=status)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "row_selection"
    assert status in aborted["reason"]
    await session.close()
    await db.close()


async def test_reverse_refuses_a_row_that_already_carries_an_exit(tmp_path):
    """An exit signature on the row means one may already be in flight."""
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db, exit_order_id="prior-exit-signature")
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        assert await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN) == (
            EXIT_REFUSED
        )
        assert _submissions(m) == []
    assert "prior-exit-signature" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_reverse_refuses_a_row_with_no_recorded_entry_fill(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db, entry_fill_qty=None)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        assert await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN) == (
            EXIT_REFUSED
        )
        assert _submissions(m) == []
    assert "entry fill" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_reverse_refuses_selling_more_than_the_row_claims(tmp_path):
    """Selling past the position is selling USDC this lane never bought."""
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        code = await runner.reverse_place(live_trade_id=row_id, usdc=Decimal("9.5"))
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "amount"
    assert _step(_steps(tmp_path), "quote") is None
    await session.close()
    await db.close()


async def test_reverse_refuses_when_another_solana_row_is_in_flight(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    await _seed_position(db, status="needs_manual_review")
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_BLOCKED
    assert _step(_steps(tmp_path), "aborted")["stage"] == "lane_state"
    await session.close()
    await db.close()


# ======================================================================
# BOUNDED_AUTONOMOUS stays refusal-only
# ======================================================================
async def test_bounded_autonomous_refuses_the_reverse_swap(tmp_path):
    """The mode is reachable, the envelope is real, the decision is absent.

    Seeded with the supervised history and the enable flag the preconditions
    require, so the refusal comes from the POLICY rather than from a gate in
    front of it — which is the only way to show the policy is what refuses.
    """
    runner, db, session = await _make_runner(
        tmp_path,
        SOLANA_MODE="BOUNDED_AUTONOMOUS",
        SOLANA_BOUNDED_AUTONOMOUS_ENABLED=True,
        SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS=1,
    )
    row_id = await _seed_position(db)
    now = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        await db._conn.execute(
            "INSERT INTO solana_executions "
            "(decision_id, state, mode, created_at, updated_at) "
            "VALUES ('historic', 'reconciled', 'SUPERVISED_LIVE', ?, ?)",
            (now, now),
        )
        await db._conn.commit()

    tx = build_reverse_swap_tx().tx_b64
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    steps = _steps(tmp_path)
    authorization = _step(steps, "authorization")
    assert authorization["method"] == "bounded_autonomous_policy"
    assert authorization["detail"] == "autonomous_policy_not_implemented"
    assert runner.loader_spy.calls == 0  # the funded key was never read
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


# ======================================================================
# The approval binding
# ======================================================================
def _snapshot(**overrides) -> dict[str, str]:
    base = dict(
        message_sha256="a" * 64,
        wallet=PAYER_PUBKEY,
        live_trade_id=5,
        row_status="open",
        usdc_in_raw=_USDC_IN_RAW,
        expected_sol_out_raw=_SOL_OUT,
        minimum_sol_out_raw=_SOL_MIN_OUT,
        route="Orca|amm|" + USDC_MINT + "|" + SOL_MINT + "|1|2",
        slippage_bps=100,
        price_impact_pct=Decimal("0.01"),
        total_fee_lamports=_TOTAL_FEE,
        priority_fee_lamports=_PRIORITY_FEE,
        jito_tip_lamports=_TIP,
        blockhash="EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k",
        close_usdc_ata=False,
    )
    base.update(overrides)
    return build_reverse_snapshot(**base)


_MUTATIONS = {
    "message_sha256": dict(message_sha256="b" * 64),
    "wallet": dict(wallet=STRANGER),
    "live_trade_id": dict(live_trade_id=6),
    "row_status": dict(row_status="needs_manual_review"),
    "usdc_in_raw": dict(usdc_in_raw=_USDC_IN_RAW + 1),
    "expected_sol_out_raw": dict(expected_sol_out_raw=_SOL_OUT + 1),
    "minimum_sol_out_raw": dict(minimum_sol_out_raw=_SOL_MIN_OUT + 1),
    "route": dict(route="Raydium|amm|" + USDC_MINT + "|" + SOL_MINT + "|1|2"),
    "slippage_bps": dict(slippage_bps=101),
    "price_impact_pct": dict(price_impact_pct=Decimal("0.02")),
    "total_fee_lamports": dict(total_fee_lamports=_TOTAL_FEE + 1),
    "priority_fee_lamports": dict(priority_fee_lamports=_PRIORITY_FEE + 1),
    "jito_tip_lamports": dict(jito_tip_lamports=_TIP + 1),
    "blockhash": dict(blockhash="11111111111111111111111111111112"),
    "close_usdc_ata": dict(close_usdc_ata=True),
}


@pytest.mark.parametrize("field", sorted(_MUTATIONS))
def test_the_digest_changes_with_every_bound_field(field):
    """Each bound field, one at a time. A field that does not move the digest
    is a field the operator was shown and did not actually approve."""
    baseline = reverse_intent_digest(_snapshot())
    mutated = reverse_intent_digest(_snapshot(**_MUTATIONS[field]))
    assert mutated != baseline, f"{field} does not move the approval digest"


def test_every_required_field_is_actually_bound():
    """The brief's list, checked against the snapshot rather than the prose."""
    assert set(_snapshot()) == set(_MUTATIONS)


def test_the_digest_is_stable_for_identical_inputs():
    assert reverse_intent_digest(_snapshot()) == reverse_intent_digest(_snapshot())
    assert reverse_authorization_token(_snapshot()) == (
        reverse_intent_digest(_snapshot())[:8]
    )


def test_the_route_fingerprint_carries_every_step_field():
    """``_route_summary`` collapses a route to AMM labels; two different routes
    through the same AMMs render identically there. The binding needs the
    fields themselves."""

    class _Step:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _Quote:
        route_plan = (
            _Step(
                label="Orca",
                amm_key="amm1",
                input_mint=USDC_MINT,
                output_mint="MID",
                in_amount=1,
                out_amount=2,
            ),
            _Step(
                label="Orca",
                amm_key="amm2",
                input_mint="MID",
                output_mint=SOL_MINT,
                in_amount=2,
                out_amount=3,
            ),
        )

    fingerprint = route_fingerprint(_Quote())
    assert fingerprint.count(";") == 1
    for token in ("amm1", "amm2", "MID", USDC_MINT, SOL_MINT):
        assert token in fingerprint

    class _Empty:
        route_plan = ()

    assert route_fingerprint(_Empty()) == "(none)"


# ======================================================================
# Gates that must refuse BEFORE the operator is ever asked
# ======================================================================
async def test_an_unknown_program_in_the_build_is_refused(tmp_path, monkeypatch):
    """A closed allowlist, applied to the reverse build's own bytes.

    Driven through the lane rather than the inspector so the assertion is the
    one that matters operationally: the run refused, the funded key was never
    read, and nothing reached the block engine.
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx(swap_program_id=STRANGER).tx_b64
    _authorize(monkeypatch)  # would abort anyway; it must never be reached
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "tx_inspection"
    assert "program_ids_allowlisted" in aborted["reason"]
    assert STRANGER in aborted["reason"]
    # The operator was never asked, so nothing could have been authorized.
    assert _step(_steps(tmp_path), "authorization") is None
    assert runner.loader_spy.calls == 0
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_a_failing_simulation_refuses_before_the_operator_is_asked(
    tmp_path, monkeypatch
):
    """Independent simulation is a gate, not a display field.

    The inspector reads the transaction's own bytes and cannot see what the
    Jupiter program does inside its CPIs; simulation is what exercises them.
    A build that fails against current chain state must not reach the prompt —
    submitting it would burn a fee to land a failure.
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx().tx_b64
    _authorize(monkeypatch)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(
            m, simulation_err={"InstructionError": [4, "ProgramFailedToComplete"]}
        )
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "simulation"
    assert "ProgramFailedToComplete" in aborted["reason"]
    assert _step(_steps(tmp_path), "simulation")["ok"] is False
    assert _step(_steps(tmp_path), "authorization") is None
    assert runner.loader_spy.calls == 0
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


# ======================================================================
# Ordering: nothing is signed before the operator says so
# ======================================================================
async def test_a_closed_stdin_aborts_before_signing(tmp_path, monkeypatch):
    """The approval is a human checkpoint or it is nothing.

    EOF is what a closed pipe, ``</dev/null`` and a non-interactive shell all
    produce, and every one of them has to abort. Asserted on the reverse path
    specifically because this is the command that SELLS a position: an
    approval boundary that could be satisfied by the absence of a human is not
    a boundary.
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx().tx_b64
    _authorize(monkeypatch)  # typed=None -> the prompt raises EOFError
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    steps = _steps(tmp_path)
    authorization = _step(steps, "authorization")
    assert authorization["outcome"] == "authorization_refused"
    assert authorization["detail"] == "no_input"
    assert authorization["funded_signer_loaded"] is False
    # Nothing past the prompt ran: no re-check, no key, no signature.
    assert _step(steps, "pre_signing_recheck") is None
    assert _step(steps, "funded_signer_loaded") is None
    assert _step(steps, "reverse_intent_persisted") is None
    assert runner.loader_spy.calls == 0
    row = await _row(db, row_id)
    assert row["status"] == "open"
    assert row["exit_order_id"] is None
    # And the lane is left clear rather than blocked on a run that never began.
    execution = await solana_lane.load_execution(
        db, _step(steps, "run_started")["decision_id"]
    )
    assert execution["state"] == "failed"
    await session.close()
    await db.close()


async def test_the_funded_key_is_not_read_before_authorization(tmp_path, monkeypatch):
    """Asserted AT THE PROMPT, not after the run.

    Checking the counter afterwards would pass on an implementation that read
    the key first and prompted second, because by then the count is 1 either
    way. The observation has to happen while the prompt is on screen.
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx().tx_b64
    seen = {}

    def _observe():
        seen["loader_calls"] = runner.loader_spy.calls

    _authorize(monkeypatch, None, on_prompt=_observe)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    assert seen["loader_calls"] == 0
    assert runner.loader_spy.calls == 0  # and still not, after the refusal
    steps = _steps(tmp_path)
    assert _step(steps, "authorization")["funded_signer_loaded"] is False
    assert _step(steps, "funded_signer_loaded") is None
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_the_typed_phrase_is_the_digest_not_the_message_hash(
    tmp_path, monkeypatch
):
    """Typing the message-hash prefix must NOT authorize a reverse swap.

    The digest binds facts the message cannot carry, so accepting the message
    hash would be accepting an approval that never saw the row identity.
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx().tx_b64
    _authorize(monkeypatch, _message_sha256(tx)[:8])
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    assert _step(_steps(tmp_path), "authorization")["detail"] == "mismatch"
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def _drive(
    tmp_path,
    monkeypatch,
    *,
    tx_b64: str | None = None,
    row_id: int | None = None,
    runner=None,
    db=None,
    session=None,
    close_usdc_ata: bool = False,
    settlement: dict | None = None,
    post_approval: dict | None = None,
    mutate_row=None,
    **overrides,
):
    """Run one reverse swap to completion, typing the correct token.

    The token cannot be precomputed by the test — it is a digest over values
    the run produces — so the prompt is answered by reading the digest the run
    itself recorded, which is also a check that the recorded one is the one
    being asked for.
    """
    if runner is None:
        runner, db, session = await _make_runner(tmp_path, **overrides)
    if row_id is None:
        row_id = await _seed_position(db)
    tx = tx_b64 or build_reverse_swap_tx().tx_b64

    def _answer():
        if mutate_row is not None:
            mutate_row()
        return None

    typed: dict[str, str] = {}

    def _fake_input(_prompt: str = "") -> str:
        _answer()
        return typed["token"]

    monkeypatch.setattr("builtins.input", _fake_input)

    # The digest is written to the evidence file immediately before the prompt,
    # so it can be read back at prompt time.
    original = solana_lane.EvidenceLog.record

    def _record(self, step, **fields):
        entry = original(self, step, **fields)
        if step == "authorization_bound":
            typed["token"] = fields["token"]
        return entry

    monkeypatch.setattr(solana_lane.EvidenceLog, "record", _record)

    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        _mock_post_approval_rpc(m, **(post_approval or {}))
        m.post(
            _SUBMIT_RE,
            payload=_rpc(_expected_signature(tx)),
            headers={"x-bundle-id": "bundle-rev"},
        )
        _mock_settlement_rpc(m, **(settlement or {}))
        code = await runner.reverse_place(
            live_trade_id=row_id, usdc=_USDC_IN, close_usdc_ata=close_usdc_ata
        )
        submissions = _submissions(m)
    return code, runner, db, session, row_id, tx, submissions


# ======================================================================
# The happy path — and it is the ONLY path that closes the row
# ======================================================================
async def test_a_finalized_and_agreeing_swap_closes_the_row(tmp_path, monkeypatch):
    code, runner, db, session, row_id, tx, submissions = await _drive(
        tmp_path, monkeypatch
    )
    assert code == EXIT_OK
    # Exactly ONE POST to the block engine, ever. No resend, no rebuild.
    assert len(submissions) == 1

    row = await _row(db, row_id)
    assert row["status"] == "closed_via_reconciliation"
    assert row["exit_order_id"] == _expected_signature(tx)

    steps = _steps(tmp_path)
    reconciliation = _step(steps, "reconciliation")
    assert reconciliation["verdict"] == "pass"
    assert reconciliation["finalized"] is True
    assert reconciliation["row_closed"] is True
    assert reconciliation["balance"]["usdc_spent_raw"] == _USDC_IN_RAW
    assert reconciliation["balance"]["meets_minimum_output"] is True

    closed = _step(steps, "row_closed")
    # SOL in, SOL out: the round trip is denominated in what was put at risk.
    # The price is off the GROSS output; the P&L is off the NET one, because a
    # price is what the swap executed at and P&L is what the wallet kept.
    assert closed["entry_sol"] == "0.1"
    assert closed["exit_sol_gross"] == "0.0945"
    assert closed["exit_sol_net"] == "0.0943948"
    assert Decimal(closed["realized_pnl_sol"]) == Decimal("-0.0056052")
    assert Decimal(closed["realized_pnl_pct"]) == Decimal("-5.6052")

    execution = await solana_lane.load_execution(
        db, _step(steps, "run_started")["decision_id"]
    )
    assert execution["state"] == "reconciled"
    assert execution["detail"]["direction"] == "usdc_to_sol"
    # The execution row deliberately does NOT own the position row: `place`'s
    # recovery sweep retires the ledger row of any execution it discards, and
    # that row here is a real position.
    assert execution["live_trade_id"] is None
    assert execution["detail"]["live_trade_id"] == row_id
    await session.close()
    await db.close()


async def test_the_route_without_a_wrapped_sol_account_also_closes_the_row(
    tmp_path, monkeypatch
):
    """The wrap/unwrap pair is an ALLOWED shape, never a required one.

    Same swap, same amounts, a route that opens and closes nothing. Only the
    rent term changes, and the reconciliation must follow it rather than
    assuming the shape it usually sees.
    """
    tx = build_reverse_swap_tx(include_wsol_account=False).tx_b64
    # No account created, so no rent leaves and none comes back.
    sol_after = _SOL_BEFORE + _SOL_OUT - _UNCONDITIONAL
    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path, monkeypatch, tx_b64=tx, settlement={"sol_after": sol_after}
    )
    assert code == EXIT_OK
    assert len(submissions) == 1
    steps = _steps(tmp_path)
    inspection = _step(steps, "tx_inspection")
    assert inspection["ata_create_count"] == 0
    assert inspection["closed_account_count"] == 0
    assert inspection["passed"] is True
    assert _step(steps, "reconciliation")["verdict"] == "pass"
    assert (await _row(db, row_id))["status"] == "closed_via_reconciliation"
    await session.close()
    await db.close()


async def test_the_intent_and_signature_are_persisted_before_submission(
    tmp_path, monkeypatch
):
    """A later process with none of this run's memory must be able to reconcile
    from the record alone — so the record has to be complete AND it has to be
    written before the bytes go out."""
    code, runner, db, session, row_id, tx, _subs = await _drive(tmp_path, monkeypatch)
    assert code == EXIT_OK
    steps = _steps(tmp_path)
    names = [s["step"] for s in steps]
    intent = _step(steps, "reverse_intent_persisted")
    assert intent is not None
    # Every field the recovery path needs, present and correct.
    assert intent["expected_signature"] == _expected_signature(tx)
    assert intent["message_sha256"] == _message_sha256(tx)
    assert intent["approval_digest"] == _step(steps, "authorization_bound")["digest"]
    assert intent["pre_submit_sol_lamports"] == _SOL_BEFORE
    assert intent["pre_submit_usdc_raw"] == _USDC_BEFORE
    assert intent["live_trade_id"] == row_id
    assert intent["last_valid_block_height"] == _LAST_VALID
    # ...and strictly before the submission.
    assert names.index("reverse_intent_persisted") < names.index("submitted")
    assert names.index("signature_persisted") < names.index("submitted")

    # The same facts are durable in the database, not only in the file.
    execution = await solana_lane.load_execution(
        db, _step(steps, "run_started")["decision_id"]
    )
    assert execution["expected_signature"] == _expected_signature(tx)
    assert execution["detail"]["pre_submit_usdc_raw"] == _USDC_BEFORE
    assert execution["detail"]["approval_digest"] == intent["approval_digest"]
    await session.close()
    await db.close()


async def test_the_balances_are_re_read_after_the_prompt(tmp_path, monkeypatch):
    """The re-read is what makes the approval bind to the state at SIGNING."""
    code, runner, db, session, row_id, _tx, _subs = await _drive(tmp_path, monkeypatch)
    assert code == EXIT_OK
    steps = _steps(tmp_path)
    names = [s["step"] for s in steps]
    assert names.index("authorization") < names.index("pre_signing_recheck")
    assert names.index("pre_signing_recheck") < names.index("funded_signer_loaded")
    recheck = _step(steps, "pre_signing_recheck")
    assert recheck["digest_matches"] is True
    assert recheck["balance_moves"] == []
    assert recheck["sol_balance_lamports"] == _SOL_BEFORE
    await session.close()
    await db.close()


# ======================================================================
# The approval is void the moment the state moves
# ======================================================================
async def test_a_balance_that_moved_between_approval_and_signing_voids_it(
    tmp_path, monkeypatch
):
    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path,
        monkeypatch,
        post_approval={"usdc_at_signing": _USDC_BEFORE - 1},
    )
    assert code == EXIT_REFUSED
    assert submissions == []
    assert runner.loader_spy.calls == 0  # never signed
    steps = _steps(tmp_path)
    invalidated = _step(steps, "authorization_invalidated")
    assert any("usdc_balance_raw" in r for r in invalidated["reasons"])
    assert (await _row(db, row_id))["status"] == "open"  # position retained
    execution = await solana_lane.load_execution(
        db, _step(steps, "run_started")["decision_id"]
    )
    assert execution["state"] == "failed"  # the lane is not left blocked
    await session.close()
    await db.close()


async def test_a_blockhash_that_expired_while_reading_voids_the_approval(
    tmp_path, monkeypatch
):
    """Binding the blockhash into the digest is not the same as refusing a
    stale one.

    The digest catches a build whose blockhash CHANGED. This catches the build
    whose blockhash is unchanged and has simply run out of time — the chain
    has passed ``lastValidBlockHeight``, so the transaction the operator
    approved can no longer land, and signing it would spend an authorization
    on bytes that are already dead.
    """
    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path,
        monkeypatch,
        # Past lastValidBlockHeight, never mind the safety margin.
        post_approval={"height": _LAST_VALID + 50},
    )
    assert code == EXIT_REFUSED
    assert submissions == []
    assert runner.loader_spy.calls == 0
    steps = _steps(tmp_path)
    recheck = _step(steps, "blockhash_recheck")
    assert recheck["valid"] is False
    assert recheck["blocks_remaining"] == -50
    # Refused BEFORE the state re-read, so the key was never anywhere near it.
    assert _step(steps, "pre_signing_recheck") is None
    assert _step(steps, "funded_signer_loaded") is None
    row = await _row(db, row_id)
    assert row["status"] == "open"
    assert row["exit_order_id"] is None
    execution = await solana_lane.load_execution(
        db, _step(steps, "run_started")["decision_id"]
    )
    assert execution["state"] == "failed"
    await session.close()
    await db.close()


async def test_a_row_that_moved_between_approval_and_signing_voids_it(
    tmp_path, monkeypatch
):
    """The row is re-read at SIGNING, not merely before the screen."""
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    moved = {"done": False}

    async def _move():
        await solana_lane.update_live_trade(db, row_id, status="needs_manual_review")

    # Mutating from inside the synchronous prompt is not possible, so the row
    # is moved by patching the prompt to schedule it and awaiting at the next
    # suspension point — which is exactly where the re-read happens.
    original = solana_lane.LaneRunner._recheck_blockhash

    async def _recheck(self, **kw):
        if not moved["done"]:
            moved["done"] = True
            await _move()
        return await original(self, **kw)

    monkeypatch.setattr(solana_lane.LaneRunner, "_recheck_blockhash", _recheck)

    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path, monkeypatch, runner=runner, db=db, session=session, row_id=row_id
    )
    assert code == EXIT_REFUSED
    assert submissions == []
    assert runner.loader_spy.calls == 0
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "pre_signing_recheck"
    assert "needs_manual_review" in aborted["reason"]
    execution = await solana_lane.load_execution(
        db, _step(_steps(tmp_path), "run_started")["decision_id"]
    )
    assert execution["state"] == "failed"
    await session.close()
    await db.close()


async def test_the_pre_signing_digest_is_recomputed_not_copied(tmp_path, monkeypatch):
    """*** THE CHECK MUST NOT BE HOLLOW. ***

    Copying the approved snapshot and overwriting the one field known to move
    would produce a comparison that can only ever detect that field — every
    other bound value would be equal to itself by construction. Here the ROUTE
    changes after the operator approved (standing in for a re-quote), and the
    recomputation has to notice.
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    real = solana_lane.route_fingerprint
    approved = {"seen": False}

    def _fingerprint(quote):
        if approved["seen"]:
            return "SomeOtherAmm|other|x|y|1|2"
        return real(quote)

    original_render = solana_lane.LaneRunner._render_reverse_decision_screen

    async def _render(self, **kw):
        result = await original_render(self, **kw)
        approved["seen"] = True
        return result

    monkeypatch.setattr(solana_lane, "route_fingerprint", _fingerprint)
    monkeypatch.setattr(
        solana_lane.LaneRunner, "_render_reverse_decision_screen", _render
    )

    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path, monkeypatch, runner=runner, db=db, session=session, row_id=row_id
    )
    assert code == EXIT_REFUSED
    assert submissions == []
    assert runner.loader_spy.calls == 0
    recheck = _step(_steps(tmp_path), "pre_signing_recheck")
    assert recheck["digest_matches"] is False
    assert any("route" in d for d in recheck["snapshot_differences"])
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_a_rebuilt_message_is_refused_rather_than_signed(tmp_path, monkeypatch):
    """A signed digest that is not the authorized one is a REFUSAL.

    The bytes the operator approved must be the bytes that get signed. Any
    difference — a rebuild, a re-quote, a mutated instruction list — has to
    stop the run rather than quietly become the transaction that is sent.
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx().tx_b64
    other = build_reverse_swap_tx(create_usdc_ata=True)

    real_sign = solana_lane.sign_transaction

    def _sign_something_else(tx_b64, keypair, *, expected_signer):
        # Stands in for the transaction having been rebuilt after approval.
        return real_sign(other.tx_b64, keypair, expected_signer=expected_signer)

    monkeypatch.setattr(solana_lane, "sign_transaction", _sign_something_else)

    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path,
        monkeypatch,
        runner=runner,
        db=db,
        session=session,
        row_id=row_id,
        tx_b64=tx,
    )
    assert code == EXIT_REFUSED
    assert submissions == []
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "signed_in_memory"
    assert "does not match the AUTHORIZED digest" in aborted["reason"]
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_the_kill_switch_after_approval_stops_the_run(tmp_path, monkeypatch):
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)

    # Tripped from the decision screen: the kill re-check runs immediately
    # after the prompt returns, so this is the window the re-check exists for.
    original_render = solana_lane.LaneRunner._render_reverse_decision_screen

    async def _render(self, **kw):
        await self._ks.trigger(
            triggered_by="manual",
            reason="operator pulled it",
            duration=timedelta(hours=1),
        )
        return await original_render(self, **kw)

    monkeypatch.setattr(
        solana_lane.LaneRunner, "_render_reverse_decision_screen", _render
    )

    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path, monkeypatch, runner=runner, db=db, session=session, row_id=row_id
    )
    assert code == EXIT_REFUSED
    assert submissions == []
    assert runner.loader_spy.calls == 0
    assert _step(_steps(tmp_path), "kill_switch_recheck")["kill_active"] is True
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


# ======================================================================
# Reconciliation gates the close
# ======================================================================
async def test_a_usdc_mismatch_does_not_close_the_row(tmp_path, monkeypatch):
    """A token debit is exact. Anything else is unexplained, full stop."""
    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path, monkeypatch, settlement={"usdc_after": 1_000}
    )
    assert code == EXIT_REVIEW
    assert len(submissions) == 1
    row = await _row(db, row_id)
    assert row["status"] == "open"
    assert row["exit_order_id"] is None
    reconciliation = _step(_steps(tmp_path), "reconciliation")
    assert reconciliation["verdict"] == "review"
    assert reconciliation["row_closed"] is False
    assert any("USDC spent" in m for m in reconciliation["mismatches"])
    await session.close()
    await db.close()


async def test_a_sol_shortfall_below_the_minimum_does_not_close_the_row(
    tmp_path, monkeypatch
):
    """The post-trade check IS the slippage guarantee: the inspector cannot
    read Jupiter's encoded threshold, so this is where it is proved."""
    short = _SOL_BEFORE + _SOL_MIN_OUT - _UNCONDITIONAL - 5_000_000
    code, runner, db, session, row_id, _tx, _subs = await _drive(
        tmp_path, monkeypatch, settlement={"sol_after": short}
    )
    assert code == EXIT_REVIEW
    row = await _row(db, row_id)
    assert row["status"] == "open"
    reconciliation = _step(_steps(tmp_path), "reconciliation")
    assert reconciliation["balance"]["meets_minimum_output"] is False
    assert any("slippage bound did not hold" in m for m in reconciliation["mismatches"])
    await session.close()
    await db.close()


async def test_a_confirmed_but_unfinalized_swap_does_not_close_the_row(
    tmp_path, monkeypatch
):
    """'confirmed' can be taken back by a fork. A position is not disposed of
    against something reversible."""
    code, runner, db, session, row_id, _tx, _subs = await _drive(
        tmp_path, monkeypatch, settlement={"status": _sig_status(status="confirmed")}
    )
    assert code == EXIT_REVIEW
    assert (await _row(db, row_id))["status"] == "open"
    reconciliation = _step(_steps(tmp_path), "reconciliation")
    assert reconciliation["finalized"] is False
    assert reconciliation["row_closed"] is False
    assert any("rather than finalized" in m for m in reconciliation["mismatches"])
    await session.close()
    await db.close()


async def test_a_transaction_that_failed_on_chain_leaves_the_position(
    tmp_path, monkeypatch
):
    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path,
        monkeypatch,
        settlement={"status": _sig_status(err={"InstructionError": [3, "Custom"]})},
    )
    assert code == EXIT_REFUSED
    assert len(submissions) == 1
    row = await _row(db, row_id)
    assert row["status"] == "open"
    assert row["exit_order_id"] is None
    assert _step(_steps(tmp_path), "failed_on_chain") is not None
    await session.close()
    await db.close()


# ======================================================================
# Ambiguity — resolve by signature, never resubmit
# ======================================================================
async def _run_ambiguous(tmp_path, monkeypatch, *, resolver_payloads):
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx().tx_b64

    typed: dict[str, str] = {}
    original = solana_lane.EvidenceLog.record

    def _record(self, step, **fields):
        entry = original(self, step, **fields)
        if step == "authorization_bound":
            typed["token"] = fields["token"]
        return entry

    monkeypatch.setattr(solana_lane.EvidenceLog, "record", _record)
    monkeypatch.setattr("builtins.input", lambda _p="": typed["token"])

    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        _mock_post_approval_rpc(m)
        m.post(_SUBMIT_RE, exception=TimeoutError())
        for payload in resolver_payloads:
            m.post(_RPC_URL, payload=payload)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        submissions = _submissions(m)
    return code, runner, db, session, row_id, tx, submissions


async def test_an_ambiguous_submission_that_never_landed_retains_the_position(
    tmp_path, monkeypatch
):
    expired = _rpc(_LAST_VALID + 50)
    code, runner, db, session, row_id, tx, submissions = await _run_ambiguous(
        tmp_path,
        monkeypatch,
        resolver_payloads=[
            _sig_status(known=False),  # sweep 1: absent
            expired,  # sweep 1: past lastValidBlockHeight
            _sig_status(known=False),  # sweep 2: absent
            expired,
            _rpc({"context": {"slot": 1}, "value": _SOL_BEFORE}),
            _token_accounts(_USDC_BEFORE),
            _token_accounts(0),
        ],
    )
    assert code == EXIT_REFUSED
    # ONE POST, ever. A rebuild would be a second signature and both can land.
    assert len(submissions) == 1
    steps = _steps(tmp_path)
    assert _step(steps, "ambiguity_definitively_not_submitted")[
        "ledger_status"
    ].startswith("open")
    row = await _row(db, row_id)
    assert row["status"] == "open"
    assert row["exit_order_id"] is None
    execution = await solana_lane.load_execution(
        db, _step(steps, "run_started")["decision_id"]
    )
    assert execution["state"] == "failed"  # both axes end
    await session.close()
    await db.close()


async def test_an_unresolved_submission_marks_the_row_and_never_resubmits(
    tmp_path, monkeypatch, capsys
):
    """The dangerous one: a transaction may exist that sold the position."""
    fresh = _rpc(_HEIGHT_FRESH)
    code, runner, db, session, row_id, tx, submissions = await _run_ambiguous(
        tmp_path,
        monkeypatch,
        resolver_payloads=[
            _sig_status(known=False),
            fresh,  # blockhash still valid: absence proves nothing
            _sig_status(known=False),
            fresh,
            _rpc({"context": {"slot": 1}, "value": _SOL_BEFORE}),
            _token_accounts(_USDC_BEFORE),
            _token_accounts(_USDC_BEFORE),
        ],
    )
    assert code == EXIT_ESCALATE
    assert len(submissions) == 1
    row = await _row(db, row_id)
    assert row["status"] == "needs_manual_review"
    assert row["exit_order_id"] is None  # NOT closed, and not claimed as closed
    out = capsys.readouterr().out
    assert "DO NOT REBUILD" in out
    await session.close()
    await db.close()


async def test_an_ambiguous_submission_that_landed_is_adopted_not_rebuilt(
    tmp_path, monkeypatch
):
    code, runner, db, session, row_id, tx, submissions = await _run_ambiguous(
        tmp_path,
        monkeypatch,
        resolver_payloads=[
            _sig_status(status="finalized"),  # the resolver finds it
            # the balances the resolver records as supplementary evidence
            _rpc({"context": {"slot": 1}, "value": _SOL_AFTER}),
            _token_accounts(_USDC_BEFORE),
            _token_accounts(0),
            _sig_status(status="finalized"),  # the confirmation poll
            _rpc(  # getTransaction
                {"slot": 283_000_455, "meta": {"fee": _UNCONDITIONAL, "err": None}}
            ),
            _rpc({"context": {"slot": 1}, "value": _SOL_AFTER}),
            _token_accounts(0),
        ],
    )
    assert code == EXIT_OK
    assert len(submissions) == 1
    steps = _steps(tmp_path)
    assert _step(steps, "ambiguity_adopted") is not None
    assert (await _row(db, row_id))["status"] == "closed_via_reconciliation"
    await session.close()
    await db.close()


# ======================================================================
# Recovery after interruption — all three points
# ======================================================================
async def _seed_reverse_execution(
    db, decision_id: str, state: str, *, live_trade_id: int, signature: str | None
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    detail = json.dumps(
        {
            "direction": "usdc_to_sol",
            "live_trade_id": live_trade_id,
            "approval_digest": "d" * 64,
            "message_sha256": "m" * 64,
            "pre_submit_sol_lamports": _SOL_BEFORE,
            "pre_submit_usdc_raw": _USDC_BEFORE,
            "amount_raw": _USDC_IN_RAW,
            "last_valid_block_height": _LAST_VALID,
        }
    )
    async with db._txn_lock:
        await db._conn.execute(
            "INSERT INTO solana_executions "
            "(decision_id, state, mode, expected_signature, detail, created_at, "
            " updated_at) VALUES (?, ?, 'SUPERVISED_LIVE', ?, ?, ?, ?)",
            (decision_id, state, signature, detail, now, now),
        )
        await db._conn.commit()


_DECISION = "6d1b345e-2821-40e2-ad83-4ecb18a06876"


async def test_recovery_after_the_intent_but_before_submission(tmp_path):
    """State alone proves nothing was sent — no network call is needed."""
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    await _seed_reverse_execution(
        db, _DECISION, "signed", live_trade_id=row_id, signature="sig-never-sent"
    )
    with aioresponses() as m:
        assert await runner.reverse_resolve(decision_id=_DECISION) == EXIT_OK
        assert list(m.requests) == []  # nobody was asked
    execution = await solana_lane.load_execution(db, _DECISION)
    assert execution["state"] == "failed"
    assert (await _row(db, row_id))["status"] == "open"  # position untouched
    await session.close()
    await db.close()


async def test_recovery_after_submission_asks_about_the_expected_signature(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    await _seed_reverse_execution(
        db,
        _DECISION,
        "submission_attempted",
        live_trade_id=row_id,
        signature="sig-in-flight",
    )
    expired = _rpc(_LAST_VALID + 50)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=expired)
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=expired)
        assert await runner.reverse_resolve(decision_id=_DECISION) == EXIT_OK
    execution = await solana_lane.load_execution(db, _DECISION)
    assert execution["state"] == "failed"
    row = await _row(db, row_id)
    assert row["status"] == "open"  # the position was never sold
    assert _step(_steps(tmp_path), "position_retained")["verdict"] == (
        "definitively_not_submitted"
    )
    await session.close()
    await db.close()


async def test_recovery_after_submission_restores_a_row_marked_for_review(tmp_path):
    """The row an ambiguity marked is put back once the verdict is definitive."""
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db, status="needs_manual_review")
    await _seed_reverse_execution(
        db,
        _DECISION,
        "submission_unknown",
        live_trade_id=row_id,
        signature="sig-in-flight",
    )
    expired = _rpc(_LAST_VALID + 50)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=expired)
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=expired)
        assert await runner.reverse_resolve(decision_id=_DECISION) == EXIT_OK
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_recovery_after_finality_reports_it_and_refuses_to_close_blind(
    tmp_path, capsys
):
    """A landed swap after an interruption is NOT auto-closed.

    Closing needs the pre-submission balances compared against the current
    ones, and after a run has already been interrupted that comparison is a
    judgement about money. The command says what happened, names the record
    that has the numbers, and leaves the row.
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    await _seed_reverse_execution(
        db, _DECISION, "finalized", live_trade_id=row_id, signature="sig-landed"
    )
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        m.post(_RPC_URL, payload=_sig_status(status="finalized"))
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": _SOL_AFTER}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        m.post(_RPC_URL, payload=_token_accounts(0))
        assert await runner.reverse_resolve(decision_id=_DECISION) == EXIT_BLOCKED
    row = await _row(db, row_id)
    assert row["status"] == "open"
    assert row["exit_order_id"] is None
    out = capsys.readouterr().out
    assert "does NOT close the ledger row" in out
    await session.close()
    await db.close()


async def test_reverse_resolve_refuses_a_forward_execution(tmp_path):
    """The two commands dispose of their rows in opposite directions."""
    runner, db, session = await _make_runner(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        await db._conn.execute(
            "INSERT INTO solana_executions "
            "(decision_id, state, mode, expected_signature, created_at, updated_at) "
            "VALUES (?, 'submission_attempted', 'SUPERVISED_LIVE', 's', ?, ?)",
            (_DECISION, now, now),
        )
        await db._conn.commit()
    with aioresponses() as m:
        assert await runner.reverse_resolve(decision_id=_DECISION) == EXIT_REFUSED
        assert list(m.requests) == []
    await session.close()
    await db.close()


async def test_reverse_status_is_read_only(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    await _seed_reverse_execution(
        db,
        _DECISION,
        "submission_attempted",
        live_trade_id=row_id,
        signature="sig-in-flight",
    )
    before = await _row(db, row_id)
    expired = _rpc(_LAST_VALID + 50)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=expired)
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=expired)
        assert await runner.reverse_status() == EXIT_BLOCKED
    assert await _row(db, row_id) == before
    assert (await solana_lane.load_execution(db, _DECISION))["state"] == (
        "submission_attempted"
    )
    await session.close()
    await db.close()


async def test_an_interrupted_reverse_run_blocks_the_next_one(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    await _seed_reverse_execution(
        db,
        _DECISION,
        "submission_attempted",
        live_trade_id=row_id,
        signature="sig-in-flight",
    )
    with aioresponses() as m:
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_BLOCKED
    assert _step(_steps(tmp_path), "aborted")["stage"] == "execution_recovery"
    await session.close()
    await db.close()


async def test_a_discarded_reverse_run_never_retires_the_position_row(tmp_path):
    """`place`'s recovery sweep retires the ledger row of what it discards.

    A reverse execution deliberately does not own the position row, so a
    crashed pre-submission run cannot mark a live position 'rejected'. This is
    the test that would catch that wiring being "tidied up".
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    await _seed_reverse_execution(
        db, _DECISION, "authorized", live_trade_id=row_id, signature=None
    )
    recovery = await runner._recover_interrupted_executions()
    assert recovery["blockers"] == 0
    assert recovery["executions"][0]["action"] == "abandoned"
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


# ======================================================================
# The USDC account closure option
# ======================================================================
async def test_the_closure_option_is_off_and_shown_as_off(
    tmp_path, monkeypatch, capsys
):
    code, runner, db, session, row_id, _tx, _subs = await _drive(tmp_path, monkeypatch)
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "USDC acct closure   : DISABLED (default)" in out
    assert "2039280 lamports" in out
    assert _step(_steps(tmp_path), "usdc_ata_closure_option")["effective"] is False
    inspection = _step(_steps(tmp_path), "tx_inspection")
    assert inspection["input_ata_closure_permitted"] is False
    assert inspection["input_ata_closed"] is False
    await session.close()
    await db.close()


async def test_the_flag_alone_cannot_enable_the_closure(tmp_path):
    """A CLI flag that widened a safety default would make the setting
    advisory — the same reason --simulate-only can only NARROW the mode."""
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    with aioresponses() as m:
        code = await runner.reverse_place(
            live_trade_id=row_id, usdc=_USDC_IN, close_usdc_ata=True
        )
        assert list(m.requests) == []  # nothing was even quoted
    assert code == EXIT_REFUSED
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "usdc_ata_closure"
    await session.close()
    await db.close()


async def test_a_build_that_closes_the_usdc_account_is_refused_when_the_option_is_off(
    tmp_path, monkeypatch
):
    """*** NEVER SILENTLY BUNDLED — enforced, not asserted. ***

    The account is empty the instant the swap completes, so an on-chain close
    succeeds and moves its rent. The old destination-only rule passed this: the
    rent goes to us.
    """
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx(close_usdc_ata=True).tx_b64
    monkeypatch.setattr("builtins.input", lambda _p="": "")
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "tx_inspection"
    assert "closed_accounts_permitted" in aborted["reason"]
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def test_the_closure_is_shown_on_its_own_line_with_the_exact_rent(
    tmp_path, monkeypatch, capsys
):
    tx = build_reverse_swap_tx(close_usdc_ata=True).tx_b64
    # Closing both accounts returns two accounts' worth of rent, so the SOL
    # actually received is higher by the USDC account's rent.
    sol_after = _SOL_BEFORE + _SOL_OUT - _UNCONDITIONAL + _ATA_RENT
    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path,
        monkeypatch,
        tx_b64=tx,
        close_usdc_ata=True,
        settlement={"sol_after": sol_after},
        SOLANA_REVERSE_CLOSE_USDC_ATA=True,
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "USDC acct closure   : ENABLED" in out
    assert "Recovers exactly 2039280 lamports" in out
    assert "This build closes it: YES" in out
    inspection = _step(_steps(tmp_path), "tx_inspection")
    assert inspection["input_ata_closed"] is True
    assert inspection["closed_account_count"] == 2
    assert inspection["recovered_rent_lamports"] == 2 * _ATA_RENT
    await session.close()
    await db.close()


async def test_enabling_the_option_changes_the_approval_digest(tmp_path, monkeypatch):
    """The option is inside the binding, so it cannot be flipped after the
    screen the operator read."""
    off = reverse_intent_digest(_snapshot(close_usdc_ata=False))
    on = reverse_intent_digest(_snapshot(close_usdc_ata=True))
    assert off != on


# ======================================================================
# The inspector, audited in BOTH directions
# ======================================================================
_OWN_USDC_ATA = derive_associated_token_address(PAYER_PUBKEY, USDC_MINT)
_OWN_WSOL_ATA = derive_associated_token_address(PAYER_PUBKEY, SOL_MINT)


def _permitted(direction, *, close_input_ata=False):
    runner = LaneRunner.__new__(LaneRunner)
    return LaneRunner._permitted_accounts(
        runner,
        signer_pubkey=PAYER_PUBKEY,
        direction=direction,
        close_input_ata=close_input_ata,
    )


async def _verify(settings_factory, tx_b64, direction, *, close_input_ata=False, **kw):
    permitted = _permitted(direction, close_input_ata=close_input_ata)
    return await verify_swap_transaction(
        tx_b64=tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(
            SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=8_500_000,
            SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=1_000_000,
            SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=1_000_000,
        ),
        input_mint=direction.input_mint,
        output_mint=direction.output_mint,
        ata_rent_lamports=_ATA_RENT,
        permitted_close_accounts=permitted["close"],
        permitted_ata_create_accounts=permitted["create"],
        **kw,
    )


def _failed(report):
    return [c.name for c in report.failures]


@pytest.mark.parametrize("with_wsol", [True, False])
async def test_the_reverse_shape_passes_with_and_without_the_wsol_account(
    settings_factory, with_wsol
):
    """Neither shape is load-bearing. Both are ACCEPTED, neither is REQUIRED."""
    tx = build_reverse_swap_tx(include_wsol_account=with_wsol).tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_REVERSE)
    assert report.passed, _failed(report)
    assert report.ata_create_count == (1 if with_wsol else 0)
    assert len(report.closed_accounts) == (1 if with_wsol else 0)
    assert report.recovered_rent_lamports == (_ATA_RENT if with_wsol else 0)


@pytest.mark.parametrize("with_wsol", [True, False])
async def test_the_forward_shape_passes_with_and_without_the_wsol_account(
    settings_factory, with_wsol
):
    tx = build_swap_tx(
        include_wrap_primitives=with_wsol, include_wrap_transfer=with_wsol
    ).tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_FORWARD)
    assert report.passed, _failed(report)


async def test_the_reverse_direction_refuses_closing_the_input_account(
    settings_factory,
):
    tx = build_reverse_swap_tx(close_usdc_ata=True).tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_REVERSE)
    assert not report.passed
    assert "closed_accounts_permitted" in _failed(report)
    check = next(c for c in report.checks if c.name == "closed_accounts_permitted")
    assert _OWN_USDC_ATA in check.detail


async def test_the_reverse_direction_permits_it_only_when_asked(settings_factory):
    tx = build_reverse_swap_tx(close_usdc_ata=True).tx_b64
    report = await _verify(
        settings_factory, tx, DIRECTION_REVERSE, close_input_ata=True
    )
    assert report.passed, _failed(report)
    assert set(report.closed_accounts) == {_OWN_WSOL_ATA, _OWN_USDC_ATA}
    assert report.recovered_rent_lamports == 2 * _ATA_RENT


async def test_closing_a_stranger_account_is_refused_in_both_directions(
    settings_factory,
):
    """An account that is not ours at all — the destination rule alone would
    have to catch this, and only if the rent went elsewhere too."""
    stranger_ata = derive_associated_token_address(STRANGER, USDC_MINT)
    for direction, builder in (
        (DIRECTION_REVERSE, build_reverse_swap_tx),
        (DIRECTION_FORWARD, build_swap_tx),
    ):
        tx = builder(
            extra_instructions=[
                token_close_account_ix(stranger_ata, PAYER_PUBKEY, PAYER_PUBKEY)
            ]
        ).tx_b64
        report = await _verify(settings_factory, tx, direction)
        assert not report.passed, direction.name
        assert "closed_accounts_permitted" in _failed(report), direction.name


async def test_the_forward_direction_may_still_close_its_own_usdc_account(
    settings_factory,
):
    """Deliberately NOT tightened away.

    Forward, the USDC account has just been paid into, so an on-chain close
    fails on a non-empty account — the danger the reverse rule exists for is
    absent, and refusing it here would narrow a proven live path against no
    captured counter-example.
    """
    tx = build_swap_tx(
        extra_instructions=[
            token_close_account_ix(_OWN_USDC_ATA, PAYER_PUBKEY, PAYER_PUBKEY)
        ]
    ).tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_FORWARD)
    assert report.passed, _failed(report)


async def test_creating_an_account_that_is_not_ours_is_refused_in_reverse(
    settings_factory,
):
    from solana_tx_builder import ata_create_ix

    stranger_ata = derive_associated_token_address(STRANGER, USDC_MINT)
    tx = build_reverse_swap_tx(
        extra_instructions=[
            ata_create_ix(PAYER_PUBKEY, stranger_ata, STRANGER, USDC_MINT, b"\x01")
        ]
    ).tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_REVERSE)
    assert not report.passed
    assert "created_ata_accounts_permitted" in _failed(report)


async def test_the_forward_create_allowlist_is_deliberately_unrestricted(
    settings_factory,
):
    """Reported as NOT CHECKED rather than passing silently.

    ``tx_inspector`` docstring fact 8 argues against an owner rule on creates
    because a route may legitimately open a PDA-owned intermediate; the rent is
    bounded by the fee ceiling instead. The check says so out loud so an
    evidence reader is never left inferring that it ran.
    """
    tx = build_swap_tx().tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_FORWARD)
    check = next(c for c in report.checks if c.name == "created_ata_accounts_permitted")
    assert check.passed
    assert "NOT CHECKED" in check.detail
    assert _permitted(DIRECTION_FORWARD)["create"] is None


async def test_both_mints_must_be_present_in_the_reverse_direction(settings_factory):
    # The wrapped-SOL account create also names the SOL mint, so it has to go
    # too — otherwise the fixture would smuggle the mint back into the keys and
    # mask the very check under test.
    tx = build_reverse_swap_tx(mints=(USDC_MINT,), include_wsol_account=False).tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_REVERSE)
    assert not report.passed
    assert "expected_mints_present" in _failed(report)


async def test_an_over_tipped_reverse_build_is_refused(settings_factory):
    tx = build_reverse_swap_tx(tip_lamports=5_000_000).tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_REVERSE)
    assert not report.passed
    assert "jito_tip_within_ceiling" in _failed(report)


async def test_a_reverse_tip_to_a_stranger_is_refused(settings_factory):
    tx = build_reverse_swap_tx(tip_destination=STRANGER).tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_REVERSE)
    assert not report.passed
    assert "transfer_destinations_known" in _failed(report)


async def test_a_second_required_signer_is_refused_in_reverse(settings_factory):
    tx = build_reverse_swap_tx(extra_signer=STRANGER).tx_b64
    report = await _verify(settings_factory, tx, DIRECTION_REVERSE)
    assert not report.passed
    assert "no_additional_signers" in _failed(report)


# ======================================================================
# Direction-aware limits
# ======================================================================
def test_the_mint_allowlists_swap_rather_than_widen(tmp_path):
    """A mint on neither configured list is still refused in both directions."""
    from scout.live.solana.limits import (
        DIRECTION_SOL_TO_USDC,
        DIRECTION_USDC_TO_SOL,
        LaneExposure,
        LimitsEngine,
    )

    engine = LimitsEngine(_settings(tmp_path))

    class _Q:
        swap_mode = "ExactIn"
        in_amount = _USDC_IN_RAW
        out_amount = _SOL_OUT
        input_mint = USDC_MINT
        output_mint = SOL_MINT
        slippage_bps = 100
        route_plan = ()

    reverse = engine.check_quote(
        _Q(),
        amount_lamports=_USDC_IN_RAW,
        price_impact_pct=Decimal("0.01"),
        exposure=LaneExposure(open_positions=1),
        direction=DIRECTION_USDC_TO_SOL,
    )
    names = {c.name: c.passed for c in reverse.checks}
    assert names["input_mint_allowed"] and names["output_mint_allowed"]
    assert names["open_position_exists_to_close"]
    assert "open_positions_within_limit" not in names

    # The same quote judged as a FORWARD one fails both, because forward means
    # SOL in and USDC out.
    forward = engine.check_quote(
        _Q(),
        amount_lamports=_USDC_IN_RAW,
        price_impact_pct=Decimal("0.01"),
        exposure=LaneExposure(open_positions=0),
        direction=DIRECTION_SOL_TO_USDC,
    )
    names = {c.name: c.passed for c in forward.checks}
    assert not names["input_mint_allowed"]
    assert not names["output_mint_allowed"]


def test_the_reverse_ata_create_ceiling_is_a_tightening(tmp_path):
    from scout.live.solana.limits import (
        DIRECTION_SOL_TO_USDC,
        DIRECTION_USDC_TO_SOL,
        LimitsEngine,
    )

    engine = LimitsEngine(_settings(tmp_path, SOLANA_PILOT_MAX_ATA_CREATES=3))

    class _R:
        priority_fee_lamports = 0
        jito_tip_lamports = 0
        total_fee_lamports = 0
        ata_create_count = 3
        ata_rent_lamports = 3 * _ATA_RENT
        program_ids = ()

    forward = engine.check_transaction(_R(), direction=DIRECTION_SOL_TO_USDC)
    reverse = engine.check_transaction(_R(), direction=DIRECTION_USDC_TO_SOL)
    assert {c.name: c.passed for c in forward.checks}["ata_creates_within_limit"]
    assert not {c.name: c.passed for c in reverse.checks}["ata_creates_within_limit"]


def test_the_reverse_balance_gate_has_two_legs(tmp_path):
    from scout.live.solana.limits import LimitsEngine

    engine = LimitsEngine(_settings(tmp_path))
    report = engine.check_reverse_balance(
        sol_lamports=_SOL_BEFORE,
        usdc_raw=_USDC_BEFORE,
        amount_raw=_USDC_IN_RAW,
        total_fee_lamports=_TOTAL_FEE,
    )
    assert report.passed
    assert {c.name for c in report.checks} == {
        "sol_covers_fees_and_headroom",
        "usdc_covers_swap_input",
    }

    # A wallet one raw unit short of the input does not cover it...
    short = engine.check_reverse_balance(
        sol_lamports=_SOL_BEFORE,
        usdc_raw=_USDC_BEFORE - 1,
        amount_raw=_USDC_IN_RAW,
        total_fee_lamports=_TOTAL_FEE,
    )
    assert not short.passed
    # ...and neither does an unreadable balance.
    unreadable = engine.check_reverse_balance(
        sol_lamports=None,
        usdc_raw=None,
        amount_raw=_USDC_IN_RAW,
        total_fee_lamports=_TOTAL_FEE,
    )
    assert not unreadable.passed


async def test_a_wallet_that_cannot_pay_the_fees_refuses_before_the_prompt(
    tmp_path, monkeypatch
):
    runner, db, session = await _make_runner(tmp_path)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx().tx_b64
    monkeypatch.setattr("builtins.input", lambda _p="": "")
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m, sol_before=1_000)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    assert _step(_steps(tmp_path), "aborted")["stage"] == "balance"
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


# ======================================================================
# The daily cap is an ENTRY cap and must never gate the exit
# ======================================================================
# The whole section runs against the SMALLEST daily cap the configuration
# permits, so the position being closed has by itself already exhausted the
# day: 7.00 USD authorized against a 10 USD cap leaves 3.10 short of the
# 6.899618 the exit quotes. Every "the exit is allowed" assertion below would
# therefore fail without the exemption, and every "the exit is refused" one
# would pass for the wrong reason if the exemption leaked.
_CAP_EXHAUSTED = dict(SOLANA_MAX_DAILY_NOTIONAL_USD=10.0)

# 0.1 SOL in at 70.0 USDC/SOL — what `_seed_position` records the row as
# holding, in the raw units the binding carries it in.
_REMAINING_RAW = 7_000_000


class _ReverseQuote:
    """The least a quote has to look like for ``check_quote`` to judge it."""

    swap_mode = "ExactIn"
    input_mint = USDC_MINT
    output_mint = SOL_MINT
    out_amount = _SOL_OUT
    slippage_bps = 100
    route_plan = ()

    def __init__(self, in_amount: int = _USDC_IN_RAW) -> None:
        self.in_amount = in_amount


class _ForwardQuote:
    """A legitimate ENTRY: SOL in, USDC out, inside the per-trade band."""

    swap_mode = "ExactIn"
    input_mint = SOL_MINT
    output_mint = USDC_MINT
    in_amount = 100_000_000
    out_amount = _USDC_IN_RAW
    slippage_bps = 100
    route_plan = ()


def _binding(**overrides) -> OperatorExitBinding:
    base = dict(
        live_trade_id=5,
        venue="solana",
        direction=DIRECTION_USDC_TO_SOL,
        remaining_usdc_raw=_REMAINING_RAW,
        authorized_by=OPERATOR_EXIT_AUTHORIZATION,
    )
    base.update(overrides)
    return OperatorExitBinding(**base)


def _cap_report(
    tmp_path,
    *,
    binding,
    quote=None,
    direction: str = DIRECTION_USDC_TO_SOL,
    open_positions: int = 1,
    authorized_today: str = "7.0",
):
    """One quote-stage verdict with the day already spent."""
    quote = quote or _ReverseQuote()
    return LimitsEngine(_settings(tmp_path, **_CAP_EXHAUSTED)).check_quote(
        quote,
        amount_lamports=quote.in_amount,
        price_impact_pct=Decimal("0.01"),
        exposure=LaneExposure(
            open_positions=open_positions,
            notional_usd_today=Decimal(authorized_today),
        ),
        direction=direction,
        exit_binding=binding,
    )


def _detail(report, name: str) -> str:
    return next(c.detail for c in report.checks if c.name == name)


async def test_an_exhausted_entry_cap_still_lets_the_position_close(
    tmp_path, monkeypatch
):
    """*** The lane must never be able to open what it cannot shut. ***

    The motivating case exactly: the day's entry allowance is gone, and the
    thing that spent it is the very position this swap closes. Under the old
    rule the exit was refused at 13.899618 against a 10 USD cap — a live
    position with no way out of it short of editing config.
    """
    code, runner, db, session, row_id, tx, submissions = await _drive(
        tmp_path, monkeypatch, **_CAP_EXHAUSTED
    )
    assert code == EXIT_OK
    assert len(submissions) == 1
    assert (await _row(db, row_id))["status"] == "closed_via_reconciliation"

    quote = _step(_steps(tmp_path), "quote")
    checks = {c["name"]: c for c in quote["limits"]["checks"]}
    # The entry cap did not silently pass — it was REPLACED, and the check that
    # replaced it says what it proved instead.
    assert "daily_notional_within_cap" not in checks
    granted = checks["operator_exit_within_bound_position"]
    assert granted["passed"] is True
    assert f"live_trades #{row_id}" in granted["detail"]
    assert "REDUCES exposure" in granted["detail"]
    assert quote["exit_binding"] == {
        "live_trade_id": row_id,
        "venue": "solana",
        "direction": DIRECTION_USDC_TO_SOL,
        "remaining_usdc_raw": _REMAINING_RAW,
        "authorized_by": OPERATOR_EXIT_AUTHORIZATION,
    }
    await session.close()
    await db.close()


def test_an_exhausted_cap_permits_a_partial_reduction(tmp_path):
    """A close does not have to be the whole position to reduce exposure."""
    report = _cap_report(tmp_path, binding=_binding(), quote=_ReverseQuote(5_000_000))
    assert report.passed, _failed(report)
    assert "spends 5000000 of the 7000000 raw USDC" in _detail(
        report, "operator_exit_within_bound_position"
    )


def test_selling_more_than_the_bound_position_earns_no_exemption(tmp_path):
    """One raw unit past the row is exposure being ADDED, not closed.

    The runner refuses this earlier still (see
    ``test_reverse_refuses_selling_more_than_the_row_claims``); this is the
    engine's own answer, so the bound holds even if that gate is ever moved.
    """
    report = _cap_report(tmp_path, binding=_binding(), quote=_ReverseQuote(7_000_001))
    assert _failed(report) == ["daily_notional_within_cap"]
    detail = _detail(report, "daily_notional_within_cap")
    assert "the entry cap applies because" in detail
    assert "INCREASE exposure" in detail


def test_a_binding_for_another_venue_earns_no_exemption(tmp_path):
    """Kraken positions close through the Kraken lane, and its rows prove
    nothing about this lane's exposure."""
    report = _cap_report(tmp_path, binding=_binding(venue="kraken"))
    assert _failed(report) == ["daily_notional_within_cap"]
    assert "'kraken' row" in _detail(report, "daily_notional_within_cap")


async def test_a_kraken_row_never_reaches_the_exemption(tmp_path):
    """And end to end, no binding is ever minted for one: the row is refused
    before a quote exists, so the exemption cannot appear at all."""
    runner, db, session = await _make_runner(tmp_path, **_CAP_EXHAUSTED)
    row_id = await _seed_position(db, venue="kraken")
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    steps = _steps(tmp_path)
    assert _step(steps, "aborted")["stage"] == "row_selection"
    assert _step(steps, "quote") is None
    assert "operator_exit_within_bound_position" not in json.dumps(steps)
    await session.close()
    await db.close()


@pytest.mark.parametrize(
    "status", ["rejected", "needs_manual_review", "closed_via_reconciliation"]
)
async def test_a_row_that_is_not_open_never_reaches_the_exemption(tmp_path, status):
    """The binding carries no status because it never has to: a row that is not
    open is refused before anything can be minted from it."""
    runner, db, session = await _make_runner(tmp_path, **_CAP_EXHAUSTED)
    row_id = await _seed_position(db, status=status)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(SOLANA_MAINNET_GENESIS_HASH))
        m.post(_RPC_URL, payload=_rpc("ok"))
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED
    steps = _steps(tmp_path)
    assert _step(steps, "aborted")["stage"] == "row_selection"
    assert _step(steps, "quote") is None
    assert "operator_exit_within_bound_position" not in json.dumps(steps)
    await session.close()
    await db.close()


def test_a_reverse_swap_with_nothing_to_close_stays_capped(tmp_path):
    """*** The exemption is for CLOSING, not for the direction. ***

    A USDC->SOL swap with no position behind it BUYS SOL: it increases the
    lane's exposure exactly as a forward entry does, and the direction it
    happens to point in must not buy it an exemption. Nothing proved it, so the
    entry cap answers — and the position check answers too.
    """
    report = _cap_report(tmp_path, binding=None, open_positions=0)
    assert _failed(report) == [
        "daily_notional_within_cap",
        "open_position_exists_to_close",
    ]
    # Nothing was offered, so nothing is explained away.
    assert "the entry cap applies because" not in _detail(
        report, "daily_notional_within_cap"
    )


def test_a_forward_entry_is_still_capped_while_holding_a_valid_binding(tmp_path):
    """The regression that would prove the exemption leaked into the entry path.

    A binding that would be honoured on the reverse leg is handed to a forward
    ENTRY. The entry is risk-increasing whatever else is true of the day, so
    the cap must be the check that answers.
    """
    report = _cap_report(
        tmp_path,
        binding=_binding(),
        quote=_ForwardQuote(),
        direction=DIRECTION_SOL_TO_USDC,
        open_positions=0,
    )
    assert "operator_exit_within_bound_position" not in {c.name for c in report.checks}
    assert "daily_notional_within_cap" in _failed(report)
    assert "judged as 'sol_to_usdc'" in _detail(report, "daily_notional_within_cap")


def test_bounded_autonomous_cannot_mint_an_exempting_binding(tmp_path):
    """*** The autonomous mode is kept out by the value, not by a comment. ***

    Both bindings are minted from the SAME proven row by the same function. The
    only thing that differs is the authorization policy the mode maps to, and
    that is the field the engine reads — so an autonomous run falls through to
    the ordinary entry cap without the engine ever being told the mode.
    """
    selection = {
        "row": {"live_trade_id": 5, "venue": "solana"},
        "ledger_usdc": Decimal("7.0"),
    }
    supervised = solana_lane.operator_exit_binding(
        selection=selection, mode="SUPERVISED_LIVE"
    )
    autonomous = solana_lane.operator_exit_binding(
        selection=selection, mode="BOUNDED_AUTONOMOUS"
    )
    assert supervised.authorized_by == OPERATOR_EXIT_AUTHORIZATION
    assert autonomous.authorized_by == "bounded_autonomous_policy"
    assert supervised.remaining_usdc_raw == autonomous.remaining_usdc_raw

    assert _cap_report(tmp_path, binding=supervised).passed
    capped = _cap_report(tmp_path, binding=autonomous)
    assert _failed(capped) == ["daily_notional_within_cap"]
    assert "bounded_autonomous_policy" in _detail(capped, "daily_notional_within_cap")


def test_the_engine_and_the_lane_agree_on_who_may_be_exempt():
    """The one string the exemption turns on, spelled in two modules.

    ``limits`` cannot import the lane (the lane imports it), so the method name
    is restated there — the same shape as ``FeeCeilings`` restating the
    inspector's ceilings, and pinned for the same reason. A rename on one side
    alone would either wedge the exit shut or, far worse, hand the exemption to
    whatever inherited the old spelling.
    """
    assert solana_lane.TypedOperatorAuthorization.method == OPERATOR_EXIT_AUTHORIZATION
    assert (
        solana_lane.BoundedAutonomousAuthorization.method != OPERATOR_EXIT_AUTHORIZATION
    )


def test_the_remaining_position_is_rounded_down_never_up(tmp_path):
    """A ledger row's USDC claim can carry more precision than USDC has units.

    Rounding to nearest would hand the exemption a raw unit the row does not
    account for, which is the one direction this number must never move in.
    """
    binding = solana_lane.operator_exit_binding(
        selection={
            "row": {"live_trade_id": 9, "venue": "solana"},
            # 0.0985 SOL at 70.0559 USDC/SOL — seven decimal places, which is
            # one more than USDC has.
            "ledger_usdc": Decimal("0.0985") * Decimal("70.0559"),
        },
        mode="SUPERVISED_LIVE",
    )
    assert binding.remaining_usdc_raw == 6_900_506  # .5 truncated, not rounded up
    assert _failed(
        _cap_report(tmp_path, binding=binding, quote=_ReverseQuote(6_900_507))
    ) == ["daily_notional_within_cap"]


async def test_the_exemption_does_not_soften_the_typed_authorization(
    tmp_path, monkeypatch
):
    """*** Exempt from the CAP, and from nothing else. ***

    The exemption is granted — the report below shows it — and the run is still
    refused, because one hex digit of the bound digest was typed wrong. The
    funded key is never read and the position stays open.
    """
    runner, db, session = await _make_runner(tmp_path, **_CAP_EXHAUSTED)
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx().tx_b64
    typed: dict[str, str] = {}
    original = solana_lane.EvidenceLog.record

    def _record(self, step, **fields):
        entry = original(self, step, **fields)
        if step == "authorization_bound":
            token = fields["token"]
            typed["token"] = ("0" if token[0] != "0" else "1") + token[1:]
        return entry

    monkeypatch.setattr(solana_lane.EvidenceLog, "record", _record)
    monkeypatch.setattr("builtins.input", lambda _p="": typed["token"])

    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_REFUSED

    steps = _steps(tmp_path)
    checks = {c["name"] for c in _step(steps, "quote")["limits"]["checks"]}
    assert "operator_exit_within_bound_position" in checks  # the exemption held
    assert _step(steps, "authorization")["outcome"] == "authorization_refused"
    assert runner.loader_spy.calls == 0
    assert (await _row(db, row_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_the_exempted_exit_still_dies_if_its_row_moves_before_signing(
    tmp_path, monkeypatch
):
    """The exemption's whole proof is the row, so the row moving voids it.

    Same guarantee as ``test_a_row_that_moved_between_approval_and_signing_
    voids_it``, re-asserted under an exhausted cap: an exit that skipped the
    entry cap must not also have skipped the re-read that establishes the row
    is still the thing being closed.
    """
    runner, db, session = await _make_runner(tmp_path, **_CAP_EXHAUSTED)
    row_id = await _seed_position(db)
    moved = {"done": False}
    original = solana_lane.LaneRunner._recheck_blockhash

    async def _recheck(self, **kw):
        if not moved["done"]:
            moved["done"] = True
            await solana_lane.update_live_trade(
                db, row_id, status="needs_manual_review"
            )
        return await original(self, **kw)

    monkeypatch.setattr(solana_lane.LaneRunner, "_recheck_blockhash", _recheck)
    code, runner, db, session, row_id, _tx, submissions = await _drive(
        tmp_path, monkeypatch, runner=runner, db=db, session=session, row_id=row_id
    )
    assert code == EXIT_REFUSED
    assert submissions == []
    assert runner.loader_spy.calls == 0
    aborted = _step(_steps(tmp_path), "aborted")
    assert aborted["stage"] == "pre_signing_recheck"
    await session.close()
    await db.close()


# ======================================================================
# Rehearsal
# ======================================================================
async def test_a_rehearsal_never_reads_the_key_and_never_touches_the_row(
    tmp_path, monkeypatch
):
    runner, db, session = await _make_runner(tmp_path, SOLANA_MODE="SIMULATION_ONLY")
    row_id = await _seed_position(db)
    tx = build_reverse_swap_tx().tx_b64
    typed: dict[str, str] = {}
    original = solana_lane.EvidenceLog.record

    def _record(self, step, **fields):
        entry = original(self, step, **fields)
        if step == "authorization_bound":
            typed["token"] = fields["token"]
        return entry

    monkeypatch.setattr(solana_lane.EvidenceLog, "record", _record)
    monkeypatch.setattr("builtins.input", lambda _p="": typed["token"])
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        code = await runner.reverse_place(live_trade_id=row_id, usdc=_USDC_IN)
        assert _submissions(m) == []
    assert code == EXIT_OK
    assert runner.loader_spy.calls == 0
    assert (
        _step(_steps(tmp_path), "rehearsal_complete")["funded_signer_loaded"] is False
    )
    assert (await _row(db, row_id))["status"] == "open"
    # A rehearsal writes no execution row: it has nothing to recover.
    assert await solana_lane.fetch_reverse_executions(db) == []
    await session.close()
    await db.close()


# ======================================================================
# CLI
# ======================================================================
def test_the_reverse_subcommands_exist_and_require_their_arguments():
    parser = build_parser()
    args = parser.parse_args(["reverse", "--live-trade-id", "5", "--usdc", "6.899618"])
    assert args.live_trade_id == 5
    assert args.usdc == Decimal("6.899618")
    assert args.close_usdc_ata is False

    with pytest.raises(SystemExit):
        parser.parse_args(["reverse", "--usdc", "1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reverse", "--live-trade-id", "5"])
    # A fractional raw unit cannot be expressed and must not be truncated.
    with pytest.raises(SystemExit):
        parser.parse_args(["reverse", "--live-trade-id", "5", "--usdc", "1.0000001"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reverse", "--live-trade-id", "5", "--usdc", "NaN"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reverse", "--live-trade-id", "5", "--usdc", "-1"])

    assert parser.parse_args(["reverse-status"]).command == "reverse-status"
    assert (
        parser.parse_args(["reverse-resolve", "--decision-id", _DECISION]).decision_id
        == _DECISION
    )


def test_the_reverse_command_takes_the_same_lock_as_place():
    """One wallet, one ledger. Two processes each holding "the other
    direction's" lock would submit two transactions against each other's
    assumptions."""
    source = Path(solana_lane.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(
        n
        for n in tree.body
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "main"
    )
    compares = [
        n
        for n in ast.walk(main)
        if isinstance(n, ast.Compare) and isinstance(n.ops[0], ast.In)
    ]
    lock_guard = [
        c
        for c in compares
        if isinstance(c.comparators[0], ast.Tuple)
        and {e.value for e in c.comparators[0].elts if isinstance(e, ast.Constant)}
        == {"place", "reverse"}
    ]
    assert lock_guard, "the reverse command must be inside the lane lock"
