"""PR-S2: the supervised Solana pilot runner.

The load-bearing assertions here are negative: no Jito POST when a gate
refuses, none when the operator types the wrong phrase, none when the kill
switch flips after approval, none when the blockhash went stale while the
operator was reading, and no REBUILD after an ambiguous submission. Where a
test asserts a refusal it also asserts the block engine was never asked,
because "refused" that still submitted a transaction is the failure this whole
module exists to prevent.

Transactions are real ``solders`` v0 objects from ``tests/solana_tx_builder.py``
— the inspector is exercised against the byte layout Jupiter actually
produces, not against a dict that would pass an inspector unable to parse a
transaction.

RPC ordering note: every Solana JSON-RPC call goes to the same URL, and
``aioresponses`` serves same-URL mocks in registration order. The helpers
below therefore register RPC payloads in the exact order ``place`` issues
them, and that order is itself part of what these tests pin down.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.db import Database
from scout.live import solana_pilot
from scout.live.kill_switch import KillSwitch
from scout.live.solana.constants import SOL_MINT, USDC_MINT
from scout.live.solana.jito_client import JitoClient
from scout.live.solana.jupiter_client import JupiterClient
from scout.live.solana.rpc_client import SolanaRpcClient
from scout.live.solana.signer import (
    REQUIRED_MODE,
    SolanaKeypairError,
    enforce_keyfile_security,
    sign_transaction,
)
from scout.live.solana_pilot import (
    EXIT_BLOCKED,
    EXIT_ESCALATE,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_REVIEW,
    PilotLockHeld,
    PilotRunner,
    build_parser,
)
from solana_tx_builder import PAYER, PAYER_PUBKEY, STRANGER, build_swap_tx

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")

# Planted into the settings so the evidence-scrubbing test has something
# key-shaped to hunt for. Not a real credential.
_PLANTED_KEYPAIR_PATH = "/srv/secrets/planted-solana-id-DO-NOT-LEAK.json"
# The fixture keypair's own secret bytes, as they would appear in an id.json.
# Asserted ABSENT from every evidence file.
_PLANTED_KEY_BYTES = json.dumps(list(bytes(PAYER)))

_QUOTE_RE = re.compile(r"https://api\.jup\.ag/swap/v1/quote.*")
_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
# A DEDICATED node, not the public round-robin. The runner refuses to place
# anything while the resolver would read from a load-balanced endpoint (see
# test_place_refuses_a_load_balanced_resolver_endpoint), so every fixture
# that is meant to get past the envelope has to name a pinned one.
_RPC_URL = "https://pilot-node.example-rpc.net/rpc"
_ROUND_ROBIN_RPC_URL = "https://api.mainnet-beta.solana.com"
_SUBMIT_RE = re.compile(
    r"https://mainnet\.block-engine\.jito\.wtf/api/v1/transactions.*"
)
_TIP_ACCOUNTS_URL = "https://mainnet.block-engine.jito.wtf/api/v1/getTipAccounts"

# 0.05 SOL in, 8.528730 USDC out — inside the default [5, 10] USD band.
_SOL = Decimal("0.05")
_LAMPORTS = 50_000_000
_OUT_RAW = 8_528_730
_MIN_OUT_RAW = 8_443_443

_LAST_VALID = 283_000_500
_HEIGHT_FRESH = 283_000_100
_ATA_RENT = 2_039_280

# What the inspector prices for the baseline fixture. The rent term is the
# one that matters: the fixture emits ONE ATA create, and tx_inspector folds
# its rent into total_fee_lamports, so the balance gate must require it
# exactly once.
_BASE_FEE = 5_000  # one required signature
_PRIORITY_FEE = 200  # 1000 micro-lamports/CU x 200_000 CU
_TIP = 100_000
_TOTAL_FEE = _BASE_FEE + _PRIORITY_FEE + _TIP + _ATA_RENT  # 2_144_480

# Balances chosen so the happy path reconciles. The wallet actually spends
# only the fees — the wSOL account's rent is returned when it is closed
# inside the same transaction — which is why the reconciliation band spans
# "swap alone" to "swap + everything the bytes can charge".
_SOL_BEFORE = 500_000_000
_TX_FEES = _BASE_FEE + _PRIORITY_FEE + _TIP  # what leaves and does not return
_SOL_AFTER = _SOL_BEFORE - _LAMPORTS - _TX_FEES


# ----------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------
def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        SOLANA_PILOT_ENABLED=True,
        SOLANA_PILOT_KEYPAIR_PATH=_PLANTED_KEYPAIR_PATH,
        SOLANA_RPC_URL=_RPC_URL,
        SOLANA_PILOT_EVIDENCE_DIR=str(tmp_path / "evidence"),
        SOLANA_SUBMISSION_SETTLE_SEC=0.0,
        SOLANA_PILOT_POLL_INTERVAL_SEC=0.0,
        SOLANA_PILOT_CONFIRM_TIMEOUT_SEC=0.05,
        SOLANA_PILOT_FINALIZE_TIMEOUT_SEC=0.05,
        DB_PATH=str(tmp_path / "pilot.db"),
    )
    base.update(overrides)
    return Settings(_env_file=None, **_REQUIRED, **base)


async def _make_runner(tmp_path, *, keypair=PAYER, **overrides):
    """Runner + db + session against a tmp DB. Caller closes db and session."""
    settings = _settings(tmp_path, **overrides)
    db = Database(tmp_path / "pilot.db")
    await db.initialize()
    session = aiohttp.ClientSession()
    runner = PilotRunner(
        settings=settings,
        db=db,
        jupiter=JupiterClient(settings, session),
        rpc=SolanaRpcClient(settings, session),
        jito=JitoClient(settings, session),
        kill_switch=KillSwitch(db),
        keypair_loader=(lambda: keypair) if keypair is not None else _refusing_loader,
    )
    return runner, db, session


def _refusing_loader():
    raise SolanaKeypairError(
        f"keypair file {_PLANTED_KEYPAIR_PATH} has mode 0o644; required 0o600"
    )


def _authorize(monkeypatch, typed: str | None = None) -> None:
    """Feed the approval prompt. ``None`` raises EOF (non-interactive stdin)."""

    def _fake_input(_prompt: str = "") -> str:
        if typed is None:
            raise EOFError
        return typed

    monkeypatch.setattr("builtins.input", _fake_input)


def _expected_signature(tx_b64: str) -> str:
    """The signature the runner will derive for this transaction."""
    return sign_transaction(tx_b64, PAYER, expected_signer=PAYER_PUBKEY).signature


def _evidence_path(tmp_path, decision_id: str):
    return tmp_path / "evidence" / f"solana_pilot_{decision_id}.json"


def _only_evidence_file(tmp_path):
    files = sorted((tmp_path / "evidence").glob("solana_pilot_*.json"))
    assert len(files) == 1, f"expected one evidence file, got {files}"
    return files[0]


def _steps(tmp_path) -> list[dict]:
    raw = _only_evidence_file(tmp_path).read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _step(steps: list[dict], name: str) -> dict | None:
    return next((s for s in steps if s["step"] == name), None)


def _step_names(steps: list[dict]) -> list[str]:
    return [s["step"] for s in steps]


def _posts_to(mocked: aioresponses, matcher) -> list:
    out = []
    for key, calls in mocked.requests.items():
        url = str(key[1])
        hit = matcher.search(url) if hasattr(matcher, "search") else url == matcher
        if hit:
            out.extend(calls)
    return out


def _submissions(mocked: aioresponses) -> list:
    return _posts_to(mocked, _SUBMIT_RE)


# ----------------------------------------------------------------------
# Venue payloads
# ----------------------------------------------------------------------
def _quote_payload(out_amount: int = _OUT_RAW, **overrides) -> dict:
    payload = {
        "inputMint": SOL_MINT,
        "inAmount": str(_LAMPORTS),
        "outputMint": USDC_MINT,
        "outAmount": str(out_amount),
        "otherAmountThreshold": str(_MIN_OUT_RAW),
        "swapMode": "ExactIn",
        "slippageBps": 100,
        "priceImpactPct": "0.0001",
        "contextSlot": 283_000_111,
        "routePlan": [
            {
                "swapInfo": {
                    "ammKey": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
                    "label": "Raydium",
                    "inputMint": SOL_MINT,
                    "outputMint": USDC_MINT,
                    "inAmount": str(_LAMPORTS),
                    "outAmount": str(out_amount),
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
            "value": {"err": err, "logs": ["Program log: ok"], "unitsConsumed": 84_000},
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


def _mock_tip_accounts(m: aioresponses) -> None:
    m.post(
        _TIP_ACCOUNTS_URL,
        payload=_rpc(["96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"]),
        repeat=True,
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
    _mock_tip_accounts(m)


def _mock_pre_approval_rpc(
    m: aioresponses,
    *,
    sol_before: int = _SOL_BEFORE,
    usdc_before: int = 0,
    simulation_err=None,
    rpc_url: str = _RPC_URL,
) -> None:
    """The RPC reads ``place`` issues between the build and the prompt.

    Order: ATA rent -> SOL balance -> USDC balance -> simulate -> the
    display-time block height on the approval screen.
    """
    m.post(rpc_url, payload=_rpc(_ATA_RENT))
    m.post(rpc_url, payload=_rpc({"context": {"slot": 1}, "value": sol_before}))
    m.post(rpc_url, payload=_token_accounts(usdc_before))
    m.post(rpc_url, payload=_simulation_ok(simulation_err))
    m.post(rpc_url, payload=_rpc(_HEIGHT_FRESH))


def _mock_post_approval_rpc(m: aioresponses, *, height: int = _HEIGHT_FRESH) -> None:
    """The blockhash re-check that runs immediately after authorization."""
    m.post(_RPC_URL, payload=_rpc(height))


def _mock_settlement_rpc(
    m: aioresponses,
    *,
    status: dict | None = None,
    sol_after: int = _SOL_AFTER,
    usdc_after: int = _OUT_RAW,
    tx_err=None,
) -> None:
    """Confirmation poll -> getTransaction -> post-trade balances."""
    m.post(_RPC_URL, payload=status or _sig_status())
    m.post(
        _RPC_URL,
        payload=_rpc({"slot": 283_000_455, "meta": {"fee": _TX_FEES, "err": tx_err}}),
    )
    m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": sol_after}))
    m.post(_RPC_URL, payload=_token_accounts(usdc_after))


def _mock_happy_path(m: aioresponses, tx_b64: str) -> None:
    _mock_quote_and_build(m, tx_b64)
    _mock_pre_approval_rpc(m)
    _mock_post_approval_rpc(m)
    m.post(
        _SUBMIT_RE,
        payload=_rpc(_expected_signature(tx_b64)),
        headers={"x-bundle-id": "bundle-xyz"},
    )
    _mock_settlement_rpc(m)


# ----------------------------------------------------------------------
# Ledger seeding
# ----------------------------------------------------------------------
async def _seed_solana_row(
    db: Database,
    *,
    decision_id: str,
    signature: str | None,
    status: str = "open",
    size_usd: str = "8.52",
    age_seconds: float = 0.0,
) -> int:
    anchor = await solana_pilot.ensure_pilot_anchor(db)
    async with db._txn_lock:
        cur = await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, status, client_order_id, entry_order_id, created_at)
               VALUES (?, 'solana-pilot', 'SOL', 'solana', 'SOL/USDC',
                       'solana_pilot', ?, ?, ?, ?, ?)""",
            (
                anchor,
                size_usd,
                status,
                decision_id,
                signature,
                (
                    datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
                ).isoformat(),
            ),
        )
        await db._conn.commit()
        return int(cur.lastrowid)


def _write_intent_evidence(
    tmp_path, decision_id: str, *, signature: str, last_valid: int | None
) -> None:
    """Reproduce the durable intent record a real run fsyncs before approving."""
    path = _evidence_path(tmp_path, decision_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "step": "intent_persisted",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "expected_signature": signature,
                    "last_valid_block_height": last_valid,
                }
            )
            + "\n"
        )


async def _row(db: Database, decision_id: str) -> dict | None:
    return await solana_pilot.fetch_row_by_decision_id(db, decision_id)


async def _count(db: Database, table: str) -> int:
    cur = await db._conn.execute(f"SELECT COUNT(*) FROM {table}")
    return (await cur.fetchone())[0]


async def _live_rows(db: Database) -> list:
    cur = await db._conn.execute(
        "SELECT status, entry_order_id FROM live_trades WHERE venue = 'solana'"
    )
    return [(r[0], r[1]) for r in await cur.fetchall()]


# ======================================================================
# Envelope gates — every one asserts nothing was submitted
# ======================================================================
async def test_place_refuses_when_pilot_disabled(tmp_path):
    runner, db, session = await _make_runner(tmp_path, SOLANA_PILOT_ENABLED=False)
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert list(m.requests) == []  # nothing was asked of any venue
    assert "SOLANA_PILOT_ENABLED" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_place_refuses_without_a_configured_keypair_path(tmp_path):
    runner, db, session = await _make_runner(tmp_path, SOLANA_PILOT_KEYPAIR_PATH="")
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert "SOLANA_PILOT_KEYPAIR_PATH" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


@pytest.mark.parametrize(
    "field,url",
    [
        ("SOLANA_RPC_URL", "https://api.devnet.solana.com"),
        ("JITO_BLOCK_ENGINE_URL", "https://testnet.block-engine.jito.wtf"),
    ],
)
async def test_place_refuses_a_non_mainnet_host(tmp_path, field, url):
    """The approved envelope is mainnet only, and .env is how you leave it."""
    runner, db, session = await _make_runner(tmp_path, **{field: url})
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert "non-mainnet" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_place_refuses_when_the_keypair_file_is_unsafe(tmp_path):
    """A loader refusal is an envelope refusal — nothing was built or sent."""
    runner, db, session = await _make_runner(tmp_path, keypair=None)
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert list(m.requests) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "keypair_custody"
    assert "0o600" in abort["reason"]
    await session.close()
    await db.close()


def test_keyfile_policy_rejects_wide_permissions_and_foreign_owners():
    """The policy itself, exercised without depending on the host honouring chmod.

    Windows does not implement POSIX modes, so a policy only reachable through
    the filesystem would first be verified on pilot day.
    """
    enforce_keyfile_security(
        mode=0o100600, file_uid=1000, current_uid=1000, path="/k/id.json"
    )
    with pytest.raises(SolanaKeypairError, match="mode"):
        enforce_keyfile_security(
            mode=0o100644, file_uid=1000, current_uid=1000, path="/k/id.json"
        )
    with pytest.raises(SolanaKeypairError, match="owned by uid"):
        enforce_keyfile_security(
            mode=0o100600, file_uid=0, current_uid=1000, path="/k/id.json"
        )
    assert REQUIRED_MODE == 0o600


async def test_place_refuses_while_the_kill_switch_is_engaged(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    await KillSwitch(db).trigger(
        triggered_by="manual", reason="drill", duration=timedelta(hours=4)
    )
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert list(m.requests) == []
    assert _step(_steps(tmp_path), "aborted")["stage"] == "kill_switch_check"
    await session.close()
    await db.close()


# ======================================================================
# Quote envelope — the USD band is enforced on the USDC output
# ======================================================================
@pytest.mark.parametrize("out_raw", [4_990_000, 10_010_000])
async def test_place_refuses_when_the_quoted_usdc_is_outside_the_band(
    tmp_path, out_raw
):
    """$4.99 and $10.01 both refuse; the band is [5, 10] on what comes back."""
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        m.get(_QUOTE_RE, payload=_quote_payload(out_amount=out_raw))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert _posts_to(m, _SWAP_URL) == []  # refused before Jupiter built anything
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "quote_envelope"
    assert "outside the approved per-swap band" in abort["reason"]
    await session.close()
    await db.close()


async def test_place_refuses_when_price_impact_exceeds_the_ceiling(tmp_path):
    """Jupiter reports priceImpactPct as a FRACTION — 0.02 is 2%, not 0.02%."""
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        m.get(_QUOTE_RE, payload=_quote_payload(priceImpactPct="0.02"))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert _posts_to(m, _SWAP_URL) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert "price impact 2" in abort["reason"]
    await session.close()
    await db.close()


async def test_unparseable_price_impact_fails_closed(tmp_path):
    """A field we cannot read is not a zero-impact route."""
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        m.get(_QUOTE_RE, payload=_quote_payload(priceImpactPct="not-a-number"))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert "price impact 100" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_place_refuses_a_quote_that_is_not_exact_in(tmp_path):
    """otherAmountThreshold is a minimum-output bound only under ExactIn."""
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        m.get(_QUOTE_RE, payload=_quote_payload(swapMode="ExactOut"))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert "ExactIn" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_place_refuses_a_quote_with_looser_slippage_than_approved(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        m.get(_QUOTE_RE, payload=_quote_payload(slippageBps=500))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert "slippageBps 500" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


# ======================================================================
# Inspection + balance + simulation
# ======================================================================
async def test_place_refuses_a_transaction_that_pays_a_stranger(tmp_path):
    """The inspector is a gate, not a report: a bad build never reaches the key."""
    runner, db, session = await _make_runner(tmp_path)
    hostile = build_swap_tx(extra_transfer_dest=STRANGER).tx_b64
    with aioresponses() as m:
        _mock_quote_and_build(m, hostile)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "tx_inspection"
    assert "transfer_destinations_known" in abort["reason"]
    inspection = _step(_steps(tmp_path), "tx_inspection")
    assert inspection["passed"] is False
    # Every check is recorded, passed or failed — the evidence shows what was
    # proved, not just what broke.
    # A floor, not the exact count: the inspector gains checks over time and
    # this test is about the evidence carrying all of them, not about how
    # many there are today.
    assert len(inspection["checks"]) >= 15
    await session.close()
    await db.close()


async def test_place_refuses_a_tip_over_the_ceiling(tmp_path):
    """The tip is re-derived from the bytes, not trusted from the request."""
    runner, db, session = await _make_runner(
        tmp_path,
        SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=50_000,
        SOLANA_PILOT_JITO_TIP_LAMPORTS=50_000,
        # Left roomy on purpose so the TIP check is the only thing that can
        # fail. A total-fee ceiling tight enough to trip on its own would make
        # the test pass for the wrong reason.
        SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=7_200_000,
    )
    # Jupiter builds a 100_000-lamport tip regardless of what we asked for.
    with aioresponses() as m:
        _mock_quote_and_build(m, build_swap_tx(tip_lamports=100_000).tx_b64)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert "jito_tip_within_ceiling" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_place_refuses_when_sol_does_not_cover_swap_fees_and_ata_rent(tmp_path):
    """The ATA rent is real money: a gate that ignores it passes a doomed tx."""
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(tmp_path)
    # Enough for the swap and the FEES, but not for the rent plus headroom.
    barely = _LAMPORTS + _TX_FEES + 1_000
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        m.post(_RPC_URL, payload=_rpc(_ATA_RENT))
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": barely}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "balance"
    assert "ATA create(s)" in abort["reason"]
    assert _step(_steps(tmp_path), "balance") is None  # never recorded a pass
    await session.close()
    await db.close()


async def test_required_balance_counts_the_ata_rent_exactly_once(tmp_path):
    """*** The double-count regression test. ***

    tx_inspector folds ATA rent into total_fee_lamports. A balance gate that
    also added its own rent figure would demand ~0.002 SOL more than the
    transaction can possibly cost — conservative, so it fails safe, but it
    refuses wallets that are in fact funded. Both cases are pinned: a build
    that creates an account, and one that does not.
    """
    creates = build_swap_tx().tx_b64  # emits one ATA create
    exists = build_swap_tx(include_wrap_primitives=False).tx_b64

    async def _required(tx_b64, sol_balance):
        runner, db, session = await _make_runner(tmp_path)
        with aioresponses() as m:
            _mock_quote_and_build(m, tx_b64)
            m.post(_RPC_URL, payload=_rpc(_ATA_RENT))
            m.post(
                _RPC_URL,
                payload=_rpc({"context": {"slot": 1}, "value": sol_balance}),
            )
            m.post(_RPC_URL, payload=_token_accounts(0))
            m.post(_RPC_URL, payload=_simulation_ok())
            m.post(_RPC_URL, payload=_rpc(_HEIGHT_FRESH))
            await runner.place(sol=_SOL)
        step = _step(_steps(tmp_path), "balance")
        await session.close()
        await db.close()
        for stale in (tmp_path / "evidence").glob("*.json"):
            stale.unlink()
        return step

    with_ata = await _required(creates, _SOL_BEFORE)
    assert with_ata["ata_create_count"] == 1
    assert with_ata["ata_rent_lamports"] == _ATA_RENT
    assert with_ata["total_fee_lamports"] == _TOTAL_FEE
    # Exactly the swap plus the priced cost — the rent appears once.
    assert with_ata["required_lamports"] == _LAMPORTS + _TOTAL_FEE
    assert with_ata["required_with_headroom_lamports"] == int(
        (_LAMPORTS + _TOTAL_FEE) * Decimal("1.10")
    )

    without_ata = await _required(exists, _SOL_BEFORE)
    assert without_ata["ata_create_count"] == 0
    assert without_ata["ata_rent_lamports"] == 0
    # A wallet that already holds the mint is not charged for an account it
    # does not need.
    assert (
        without_ata["required_lamports"] == _LAMPORTS + _BASE_FEE + _PRIORITY_FEE + _TIP
    )


async def test_ata_rent_falls_back_and_says_so(tmp_path, monkeypatch):
    """An RPC hiccup on rent must not block the pilot, but must be visible."""
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(tmp_path)

    async def _unreachable(_data_length):
        raise TimeoutError("rent lookup timed out")

    monkeypatch.setattr(
        runner._rpc, "get_minimum_balance_for_rent_exemption", _unreachable
    )
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        # Too little SOL, so the gate refuses and names the rent figure it used.
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": 1_000}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    # The fallback is a DEGRADED reading, so the evidence has to say which
    # figure priced the ceiling and the balance gate.
    inspection = _step(_steps(tmp_path), "tx_inspection")
    assert inspection["ata_rent_source"] == "documented_fallback"
    assert inspection["ata_rent_per_account_lamports"] == _ATA_RENT
    await session.close()
    await db.close()


async def test_place_refuses_before_approval_when_simulation_fails(tmp_path, capsys):
    """A failing simulation must never reach the operator as a decision."""
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m, simulation_err={"InstructionError": [3, "Custom"]})
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    steps = _steps(tmp_path)
    assert _step(steps, "aborted")["stage"] == "simulation"
    # The approval prompt was never printed and no signature was derived.
    assert "MANUAL APPROVAL REQUIRED" not in capsys.readouterr().out
    assert "signed_in_memory" not in _step_names(steps)
    assert await _count(db, "live_trades") == 0
    await session.close()
    await db.close()


# ======================================================================
# S1-review requirement 1: the runner IS the inspect-then-sign coupling
# ======================================================================
async def test_a_failing_inspection_means_no_signature_is_ever_derived(tmp_path):
    """*** The coupling S1 structurally cannot enforce. ***

    ``sign_transaction`` does not require a passing VerificationReport and its
    ``expected_signer`` defaults to None, so on its own it will sign a message
    whose fee payer is a different key. Nothing in the venue layer stops that —
    the runner is the only place the two can be bound together. This test
    proves the binding holds in the direction that matters: a report that fails
    means the key is never asked to sign anything at all.
    """
    hostile = build_swap_tx(extra_transfer_dest=STRANGER).tx_b64
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, hostile)
        m.post(_RPC_URL, payload=_rpc(_ATA_RENT))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    names = _step_names(steps)
    # No signature was derived...
    assert "signed_in_memory" not in names
    # ...the simulation gate was never even reached...
    assert "simulation" not in names
    # ...no row asserts a transaction...
    assert await _count(db, "live_trades") == 0
    # ...and no signature string appears anywhere in the evidence.
    raw = _only_evidence_file(tmp_path).read_text(encoding="utf-8")
    assert "expected_signature" not in raw
    await session.close()
    await db.close()


def test_sign_transaction_cannot_be_called_without_an_expected_signer():
    """*** The type-level guarantee, which is the real one. ***

    ``expected_signer`` is keyword-only with NO default, so a call that omits
    it does not run at all. That holds for every caller in every module,
    including ones reached through an alias or getattr — which is exactly
    what a source-level lint cannot see.
    """
    import inspect

    from scout.live.solana.signer import sign_transaction as real_signer

    param = inspect.signature(real_signer).parameters["expected_signer"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty

    with pytest.raises(TypeError, match="expected_signer"):
        real_signer(build_swap_tx().tx_b64, PAYER)


def test_the_runner_never_calls_the_signer_without_an_expected_signer():
    """A cheap EXTRA on top of the required keyword above, not the guarantee.

    It reads only the direct-call form in this one module: an alias
    (``_s = sign_transaction``), a ``getattr(mod, "sign_transaction")(...)``
    and every call from another module are all invisible to it. Kept because
    it costs nothing and localises a regression to this file, but the
    signature is what actually enforces the rule.
    """
    import ast
    import inspect

    from scout.live import solana_pilot as module

    tree = ast.parse(inspect.getsource(module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None))
        == "sign_transaction"
    ]
    assert calls, "expected to find the signing call site"
    for call in calls:
        assert "expected_signer" in {
            kw.arg for kw in call.keywords
        }, f"sign_transaction called without expected_signer at line {call.lineno}"


async def test_the_inspected_bytes_and_the_signed_bytes_must_be_the_same(
    tmp_path, monkeypatch
):
    """The digest the approval screen shows must be the digest that got signed.

    Without this, the inspection could describe one transaction while the
    signature committed to another — the report would be true and irrelevant.
    """
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(tmp_path)
    real_sign = solana_pilot.sign_transaction

    def _sign_something_else(tx_b64, keypair, **kwargs):
        # Signs a DIFFERENT transaction with the same key, so the signature
        # is valid and the digest simply does not match what was inspected.
        return real_sign(
            build_swap_tx(tip_lamports=99_999).tx_b64,
            keypair,
            expected_signer=PAYER_PUBKEY,
        )

    monkeypatch.setattr(solana_pilot, "sign_transaction", _sign_something_else)
    _authorize(monkeypatch, "whatever")
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "signed_in_memory"
    assert "does not match the inspected digest" in abort["reason"]
    assert await _count(db, "live_trades") == 0
    await session.close()
    await db.close()


# ======================================================================
# S1-review requirements 4 + 5: the inspector gets live inputs, not defaults
# ======================================================================
async def test_the_live_jito_tip_list_is_used_and_its_provenance_recorded(
    tmp_path, monkeypatch
):
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_happy_path(m, tx)
        assert await runner.place(sol=_SOL) == EXIT_OK
    inspection = _step(_steps(tmp_path), "tx_inspection")
    assert inspection["tip_accounts_source"] == "live_block_engine"
    assert inspection["tip_accounts_checked"] == 1  # the mocked live list
    await session.close()
    await db.close()


async def test_a_static_tip_list_is_recorded_as_the_degraded_mode(tmp_path):
    """Falling back is allowed; failing to SAY so is not.

    Jito rotates its tip accounts. A run that silently checked the tip against
    a stale constant would look identical in the evidence to one that checked
    it against the live list.
    """
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        m.get(_QUOTE_RE, payload=_quote_payload())
        m.post(
            _SWAP_URL,
            payload={
                "swapTransaction": tx,
                "lastValidBlockHeight": _LAST_VALID,
                "prioritizationFeeLamports": 200,
            },
        )
        # The block engine cannot be read, so the client falls back.
        m.post(_TIP_ACCOUNTS_URL, exception=TimeoutError(), repeat=True)
        m.post(_RPC_URL, payload=_rpc(_ATA_RENT))
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": 1_000}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        await runner.place(sol=_SOL)
        assert _submissions(m) == []
    inspection = _step(_steps(tmp_path), "tx_inspection")
    assert inspection["tip_accounts_source"] == "static_fallback"
    # The tip still verified against the fallback list, so the run got as far
    # as the balance gate rather than failing inspection.
    assert inspection["passed"] is True
    await session.close()
    await db.close()


async def test_a_lookup_table_route_is_approvable_because_a_real_rpc_is_passed(
    tmp_path, monkeypatch
):
    """Without an rpc_client the inspector fails ALT routes by construction.

    ``lookup_tables_resolved`` fails, and with it the program-allowlist and
    mint-presence checks that read the resolved keys — so a runner that did not
    pass one could never approve a route using address lookup tables, which
    Jupiter routes commonly do.
    """
    import base64 as _b64

    from solders.pubkey import Pubkey

    from scout.live.solana.rpc_client import LOOKUP_TABLE_META_SIZE
    from solana_tx_builder import ALT

    # The ALT holds both mints, so compiling against it moves them OUT of the
    # static account keys — the mint-presence check can only pass once the
    # table has actually been fetched and decoded.
    built = build_swap_tx(lookup_tables=[ALT])
    table_bytes = bytes(LOOKUP_TABLE_META_SIZE) + b"".join(
        bytes(Pubkey.from_string(m)) for m in (SOL_MINT, USDC_MINT)
    )
    _authorize(monkeypatch, _expected_signature(built.tx_b64)[:8])

    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, built.tx_b64)
        m.post(_RPC_URL, payload=_rpc(_ATA_RENT))
        # The inspector resolves the table before it can judge the route.
        m.post(
            _RPC_URL,
            payload=_rpc(
                {
                    "context": {},
                    "value": [
                        {"data": [_b64.b64encode(table_bytes).decode(), "base64"]}
                    ],
                }
            ),
        )
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": _SOL_BEFORE}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        m.post(_RPC_URL, payload=_simulation_ok())
        m.post(_RPC_URL, payload=_rpc(_HEIGHT_FRESH))
        _mock_post_approval_rpc(m)
        m.post(_SUBMIT_RE, payload=_rpc(_expected_signature(built.tx_b64)))
        _mock_settlement_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_OK

    inspection = _step(_steps(tmp_path), "tx_inspection")
    assert inspection["rpc_client_supplied"] is True
    assert inspection["unresolved_lookup_tables"] == []
    assert "lookup_tables_resolved" not in inspection["failed_checks"]
    assert inspection["passed"] is True
    await session.close()
    await db.close()


# ======================================================================
# S1-review requirement 6: the resolver reads from ONE pinned node
# ======================================================================
async def test_place_refuses_a_load_balanced_resolver_endpoint(tmp_path):
    """*** The only way to manufacture a false definitively_not_submitted. ***

    The verdict combines two facts read in two calls: the signature is absent,
    AND the block height has passed lastValidBlockHeight. Behind a load
    balancer those can come from different nodes, and one that is ahead on
    height while missing the signature produces a verdict that retires a live
    row and invites a rerun — two swaps for one authorization.
    """
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_RPC_URL=_ROUND_ROBIN_RPC_URL
    )
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert list(m.requests) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "envelope_gate"
    assert "load-balanced" in abort["reason"]
    assert "SOLANA_RESOLVER_RPC_URL" in abort["reason"]
    await session.close()
    await db.close()


async def test_simulate_only_runs_on_an_unpinned_resolver_and_says_so(
    tmp_path, monkeypatch, capsys
):
    """*** The pin gates SUBMISSION, so it must not gate a rehearsal. ***

    A --simulate-only run provably never submits, so it can never produce an
    ambiguous submission, so the resolver is never consulted. Refusing one for
    the quality of an endpoint it will not read blocks the only rehearsal that
    exercises the real Jupiter/Jito/RPC path.

    It still has to SAY so: a rehearsal that passed on an unpinned resolver
    must not be mistaken for a launch-ready lane.
    """
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_RPC_URL=_ROUND_ROBIN_RPC_URL
    )
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m, rpc_url=_ROUND_ROBIN_RPC_URL)
        assert await runner.place(sol=_SOL, simulate_only=True) == EXIT_OK
        # The property the relaxation rests on.
        assert _submissions(m) == []

    out = capsys.readouterr().out
    assert "resolver endpoint is NOT pinned" in out
    assert "a real placement will refuse" in out
    assert "NOT LAUNCH-READY" in out  # on the approval screen itself

    envelope = _step(_steps(tmp_path), "envelope_gate")
    assert envelope["resolver_endpoint_pinned"] is False
    assert envelope["resolver_pin_waived_for_rehearsal"] is True
    # A rehearsal still writes no ledger row.
    assert await _count(db, "live_trades") == 0
    await session.close()
    await db.close()


async def test_simulate_only_on_a_pinned_resolver_says_nothing(
    tmp_path, monkeypatch, capsys
):
    """The notice is about a real gap, so it must not cry wolf."""
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL, simulate_only=True) == EXIT_OK
        assert _submissions(m) == []

    out = capsys.readouterr().out
    assert "NOT pinned" not in out
    assert "NOT LAUNCH-READY" not in out
    envelope = _step(_steps(tmp_path), "envelope_gate")
    assert envelope["resolver_endpoint_pinned"] is True
    assert envelope["resolver_pin_waived_for_rehearsal"] is False
    await session.close()
    await db.close()


async def test_the_jito_client_is_unreachable_from_any_rehearsal_path(
    tmp_path, monkeypatch
):
    """Belt to the AST test's braces: make submission itself explode.

    The relaxation is only safe because --simulate-only cannot submit. Patched
    at the CLASS, so any route to it — the runner's client, a second client, a
    retry — trips the same wire.
    """

    def _detonate(*_args, **_kwargs):
        raise AssertionError("a rehearsal reached the block engine")

    monkeypatch.setattr(JitoClient, "submit_transaction", _detonate)

    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    for pinned, overrides in (
        (True, {}),
        (False, {"SOLANA_RPC_URL": _ROUND_ROBIN_RPC_URL}),
    ):
        workdir = tmp_path / f"pinned_{pinned}"
        workdir.mkdir()
        runner, db, session = await _make_runner(workdir, **overrides)
        rpc_url = _RPC_URL if pinned else _ROUND_ROBIN_RPC_URL
        with aioresponses() as m:
            _mock_quote_and_build(m, tx)
            _mock_pre_approval_rpc(m, rpc_url=rpc_url)
            assert await runner.place(sol=_SOL, simulate_only=True) == EXIT_OK
        await session.close()
        await db.close()


def test_no_rehearsal_path_can_reach_the_submit_step():
    """Structural: `place` returns on the rehearsal branch before submitting.

    Same shape as the rpc_client no-broadcast test — an AST walk rather than a
    grep, because the module's prose says "the submission step is skipped" in
    several places and text matching cannot tell prose from control flow.
    """
    import ast
    import inspect

    from scout.live import solana_pilot as module

    tree = ast.parse(inspect.getsource(module))
    place = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "place"
    )

    submit_lines = [
        node.lineno
        for node in ast.walk(place)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "_submit_and_resolve"
    ]
    assert submit_lines, "expected to find the submission call site"

    # An `if simulate_only:` whose body returns — the guard itself.
    guard_returns = [
        stmt.lineno
        for node in ast.walk(place)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "simulate_only"
        for stmt in node.body
        if isinstance(stmt, ast.Return)
    ]
    assert guard_returns, "no `if simulate_only: ... return` guard in place()"
    assert min(guard_returns) < min(submit_lines), (
        "the rehearsal guard must return BEFORE the submission call, "
        f"got guard at {min(guard_returns)} and submit at {min(submit_lines)}"
    )


async def test_an_explicit_resolver_url_satisfies_the_pin(tmp_path):
    """A round-robin general RPC is fine as long as resolution is pinned."""
    runner, db, session = await _make_runner(
        tmp_path,
        SOLANA_RPC_URL=_ROUND_ROBIN_RPC_URL,
        SOLANA_RESOLVER_RPC_URL=_RPC_URL,
    )
    with aioresponses() as m:
        m.get(_QUOTE_RE, payload=_quote_payload(out_amount=4_990_000))
        # Gets past the envelope and refuses on the band instead.
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
    envelope = _step(_steps(tmp_path), "envelope_gate")
    assert envelope["resolver_endpoint_pinned"] is True
    assert envelope["resolver_rpc_url"] == _RPC_URL
    await session.close()
    await db.close()


async def test_resolve_reports_but_will_not_retire_on_an_unpinned_verdict(
    tmp_path, capsys
):
    """Reporting a verdict is always allowed; ACTING on this one is not."""
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a0688a"
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_RPC_URL=_ROUND_ROBIN_RPC_URL
    )
    await _seed_solana_row(db, decision_id=decision_id, signature=signature)
    _write_intent_evidence(
        tmp_path, decision_id, signature=signature, last_valid=_LAST_VALID
    )
    with aioresponses() as m:
        for _ in range(2):
            m.post(_ROUND_ROBIN_RPC_URL, payload=_sig_status(known=False))
            m.post(_ROUND_ROBIN_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        assert await runner.resolve(decision_id=decision_id) == EXIT_OK

    steps = _steps(tmp_path)
    assert _step(steps, "resolution")["verdict"] == "definitively_not_submitted"
    assert _step(steps, "resolution")["resolver_endpoint_pinned"] is False
    assert _step(steps, "auto_retire_withheld") is not None
    assert _step(steps, "row_auto_retired") is None
    # The row survives — that is the whole point.
    assert (await _row(db, decision_id))["status"] == "open"
    assert "NOT PINNED" in capsys.readouterr().out
    await session.close()
    await db.close()


def test_resolver_endpoint_prefers_the_explicit_url_and_flags_round_robins(
    tmp_path,
):
    from scout.live.solana_pilot import resolver_endpoint

    pinned = _settings(tmp_path, SOLANA_RPC_URL=_RPC_URL)
    assert resolver_endpoint(pinned) == (_RPC_URL, True)

    loose = _settings(tmp_path, SOLANA_RPC_URL=_ROUND_ROBIN_RPC_URL)
    assert resolver_endpoint(loose) == (_ROUND_ROBIN_RPC_URL, False)

    override = _settings(
        tmp_path,
        SOLANA_RPC_URL=_ROUND_ROBIN_RPC_URL,
        SOLANA_RESOLVER_RPC_URL=_RPC_URL,
    )
    assert resolver_endpoint(override) == (_RPC_URL, True)


# ======================================================================
# The approval boundary binds to the signature
# ======================================================================
@pytest.mark.parametrize("typed", [None, "", "deadbeef", "DEADBEEF"])
async def test_wrong_authorization_submits_nothing_and_retires_the_row(
    tmp_path, monkeypatch, typed
):
    """Anything but the exact prefix aborts, and the intent row is retired."""
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, typed)
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    assert _step(steps, "authorization")["outcome"] == "authorization_refused"
    # The row existed (it must, before submission is possible) and is now
    # retired, so it does not block the next run.
    assert _step(steps, "intent_persisted") is not None
    assert _step(steps, "intent_retired")["ledger_status"] == "rejected"
    assert await _live_rows(db) == [("rejected", _expected_signature(tx))]
    await session.close()
    await db.close()


async def test_the_authorization_phrase_is_the_signature_prefix(
    tmp_path, monkeypatch, capsys
):
    """Not a uuid: any rebuild changes the signature and so changes the phrase."""
    tx = build_swap_tx().tx_b64
    signature = _expected_signature(tx)
    _authorize(monkeypatch, signature[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_happy_path(m, tx)
        assert await runner.place(sol=_SOL) == EXIT_OK
        assert len(_submissions(m)) == 1

    out = capsys.readouterr().out
    assert f"EXPECTED SIGNATURE  : {signature}" in out
    assert "first 8 characters of the EXPECTED SIGNATURE" in out
    steps = _steps(tmp_path)
    assert _step(steps, "authorization")["bound_to_signature"] == signature
    await session.close()
    await db.close()


async def test_a_signature_from_a_different_build_does_not_authorize(
    tmp_path, monkeypatch
):
    """The prefix of some OTHER transaction's signature is just a wrong phrase."""
    tx = build_swap_tx().tx_b64
    other = build_swap_tx(tip_lamports=99_999).tx_b64
    assert _expected_signature(other) != _expected_signature(tx)
    _authorize(monkeypatch, _expected_signature(other)[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    await session.close()
    await db.close()


# ======================================================================
# Post-approval gates
# ======================================================================
async def test_kill_switch_recheck_retires_the_row_and_sends_nothing(
    tmp_path, monkeypatch
):
    """The kill is clear at step 2 and engaged by the post-approval re-check.

    That window — unbounded operator time at the prompt — is the entire
    reason the re-check exists.
    """
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(tmp_path)
    original = runner._ks.is_active
    calls = {"n": 0}

    async def _clear_then_active():
        calls["n"] += 1
        if calls["n"] == 1:
            return await original()  # step 2: clear
        from scout.live.types import KillState

        return KillState(
            kill_event_id=7,
            triggered_by="manual",
            reason="engaged mid-approval",
            killed_until=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(runner._ks, "is_active", _clear_then_active)
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    assert _step(steps, "kill_switch_recheck")["kill_active"] is True
    assert await _live_rows(db) == [("rejected", _expected_signature(tx))]
    await session.close()
    await db.close()


async def test_stale_blockhash_invalidates_the_authorization(
    tmp_path, monkeypatch, capsys
):
    """The operator read the screen too slowly; the numbers went stale."""
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        # Inside the 15-block safety margin: 283_000_490 > 283_000_500 - 15.
        _mock_post_approval_rpc(m, height=_LAST_VALID - 5)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    assert _step(steps, "blockhash_recheck")["valid"] is False
    assert _step(steps, "authorization_invalidated")["ledger_status"] == "rejected"
    assert await _live_rows(db) == [("rejected", _expected_signature(tx))]
    out = capsys.readouterr().out
    assert "AUTHORIZATION INVALIDATED" in out
    assert "a NEW quote, a NEW signature, and a NEW" in out
    await session.close()
    await db.close()


async def test_unreadable_block_height_after_approval_fails_closed(
    tmp_path, monkeypatch
):
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        m.post(_RPC_URL, exception=TimeoutError())
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert _step(_steps(tmp_path), "blockhash_recheck")["valid"] is False
    await session.close()
    await db.close()


# ======================================================================
# Rehearsal
# ======================================================================
def test_simulate_only_requires_the_rehearsal_flag():
    args = build_parser().parse_args(["place", "--sol", "0.05", "--simulate-only"])
    assert args.simulate_only and not args.yes_i_am_rehearsing


async def test_rehearsal_flags_are_cross_checked(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(solana_pilot, "load_settings", lambda: _settings(tmp_path))
    assert (
        await solana_pilot.main(["place", "--sol", "0.05", "--simulate-only"])
        == EXIT_REFUSED
    )
    assert "requires --yes-i-am-rehearsing" in capsys.readouterr().out
    assert (
        await solana_pilot.main(["place", "--sol", "0.05", "--yes-i-am-rehearsing"])
        == EXIT_REFUSED
    )
    assert "This would be a REAL swap" in capsys.readouterr().out


async def test_simulate_only_signs_prompts_and_submits_nothing(
    tmp_path, monkeypatch, capsys
):
    """The rehearsal is the real flow minus the one irreversible step."""
    tx = build_swap_tx().tx_b64
    signature = _expected_signature(tx)
    _authorize(monkeypatch, signature[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL, simulate_only=True) == EXIT_OK
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    names = _step_names(steps)
    # It really did everything else: inspected, simulated, signed, prompted.
    assert "tx_inspection" in names and "simulation" in names
    assert _step(steps, "signed_in_memory")["expected_signature"] == signature
    assert _step(steps, "authorization")["outcome"] == "authorized"
    # And left NO ledger row — a row would be a phantom blocking the next run.
    assert "intent_persisted" not in names
    assert _step(steps, "intent_skipped")["would_be_expected_signature"] == signature
    assert await _count(db, "live_trades") == 0
    assert "REHEARSAL COMPLETE" in capsys.readouterr().out
    await session.close()
    await db.close()


# ======================================================================
# Happy path
# ======================================================================
async def test_happy_path_submits_confirms_finalizes_and_reconciles(
    tmp_path, monkeypatch, capsys
):
    tx = build_swap_tx().tx_b64
    signature = _expected_signature(tx)
    _authorize(monkeypatch, signature[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_happy_path(m, tx)
        assert await runner.place(sol=_SOL) == EXIT_OK
        submissions = _submissions(m)
        assert len(submissions) == 1
        # bundleOnly=true is what buys revert protection.
        assert "bundleOnly=true" in str(submissions[0].kwargs.get("params", "")) or (
            submissions[0].kwargs.get("params", {}).get("bundleOnly") == "true"
        )

    steps = _steps(tmp_path)
    names = _step_names(steps)
    for expected in (
        "run_started",
        "envelope_gate",
        "kill_switch_check",
        "keypair_custody",
        "startup_reconciliation",
        "quote",
        "swap_built",
        "tx_inspection",
        "balance",
        "simulation",
        "signed_in_memory",
        "intent_persisted",
        "authorization",
        "kill_switch_recheck",
        "blockhash_recheck",
        "submitted",
        "confirmation",
        "transaction_record",
        "reconciliation",
    ):
        assert expected in names, f"missing evidence step {expected}"

    assert _step(steps, "confirmation")["outcome"] == "finalized"
    reconciliation = _step(steps, "reconciliation")
    assert reconciliation["verdict"] == "pass"
    assert reconciliation["balance"]["usdc_received_raw"] == _OUT_RAW

    row = await _row(db, _step(steps, "run_started")["decision_id"])
    # A landed swap leaves the row open: the position is the operator's to
    # dispose of, and it blocks the next run until they do.
    assert row["status"] == "open"
    assert row["entry_order_id"] == signature
    assert row["entry_fill_qty"] == "0.05"
    assert Decimal(row["entry_fill_price"]) == Decimal("8.528730") / Decimal("0.05")
    assert "PILOT SWAP FINALIZED" in capsys.readouterr().out
    await session.close()
    await db.close()


async def test_the_expected_signature_is_persisted_before_the_submission_step(
    tmp_path, monkeypatch
):
    """The idempotency key must be durable before anything can be sent."""
    tx = build_swap_tx().tx_b64
    signature = _expected_signature(tx)
    _authorize(monkeypatch, signature[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_happy_path(m, tx)
        assert await runner.place(sol=_SOL) == EXIT_OK

    names = _step_names(_steps(tmp_path))
    intent = _step(_steps(tmp_path), "intent_persisted")
    assert intent["expected_signature"] == signature
    assert intent["last_valid_block_height"] == _LAST_VALID
    assert names.index("intent_persisted") < names.index("submitted")
    # ...and before the operator was even asked.
    assert names.index("intent_persisted") < names.index("authorization")
    await session.close()
    await db.close()


async def test_reconciliation_records_the_minimum_output_comparison(
    tmp_path, monkeypatch
):
    """*** The post-trade check IS the slippage guarantee. ***

    tx_inspector cannot verify the minimum-output bound — the route and its
    otherAmountThreshold are encoded inside Jupiter's instruction data,
    which is opaque to a static inspector. What protects the trade is
    Jupiter's on-chain program enforcing that threshold plus this
    reconciliation confirming it held. So the comparison is recorded as an
    explicit boolean rather than left to be inferred from the absence of a
    complaint.
    """
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_happy_path(m, tx)
        assert await runner.place(sol=_SOL) == EXIT_OK

    balance = _step(_steps(tmp_path), "reconciliation")["balance"]
    assert balance["meets_minimum_output"] is True
    assert balance["usdc_minimum_raw"] == _MIN_OUT_RAW
    assert balance["usdc_received_raw"] == _OUT_RAW
    # Received more than the floor and no more than the quote: the bound
    # held, and the comparison that says so is in the evidence.
    assert _MIN_OUT_RAW <= balance["usdc_received_raw"] <= balance["usdc_quoted_raw"]
    await session.close()
    await db.close()


async def test_the_explained_sol_band_is_exactly_as_wide_as_the_rent(
    tmp_path, monkeypatch
):
    """*** Both ends of the band, for both ATA cases. ***

    The floor is the swap plus the UNCONDITIONAL costs — base signature fee,
    priority fee and tip always leave the wallet. Anchoring it at the bare
    swap amount would accept a spend with zero fees paid, which cannot happen
    on-chain; and this reconciliation is the evidence the slippage guarantee
    rests on, so it has to be as tight as the facts allow.

    The ceiling adds the ATA rent, the one genuinely conditional term — it
    comes back when an account Jupiter opened is closed inside the same
    transaction. So the band's WIDTH is the rent, and nothing else.
    """
    unconditional = _BASE_FEE + _PRIORITY_FEE + _TIP  # 105_200

    async def _band(tx_b64, *, sol_after, workdir):
        # A landed swap leaves an `open` row on purpose, and that row blocks
        # the next placement — so each case gets its own database rather than
        # having its predecessor's position deleted out from under it.
        workdir.mkdir()
        _authorize(monkeypatch, _expected_signature(tx_b64)[:8])
        runner, db, session = await _make_runner(workdir)
        with aioresponses() as m:
            _mock_quote_and_build(m, tx_b64)
            _mock_pre_approval_rpc(m)
            _mock_post_approval_rpc(m)
            m.post(_SUBMIT_RE, payload=_rpc(_expected_signature(tx_b64)))
            _mock_settlement_rpc(m, sol_after=sol_after)
            assert await runner.place(sol=_SOL) == EXIT_OK
        record = _step(_steps(workdir), "reconciliation")
        await session.close()
        await db.close()
        return record

    # One ATA create: the rent widens the ceiling but not the floor.
    creates = await _band(
        build_swap_tx().tx_b64, sol_after=_SOL_AFTER, workdir=tmp_path / "creates"
    )
    low, high = creates["balance"]["sol_spent_explained_range"]
    assert low == _LAMPORTS + unconditional - _BASE_FEE
    assert high == _LAMPORTS + unconditional + _ATA_RENT + _BASE_FEE
    assert (low, high) == (50_100_200, 52_149_480)
    assert creates["verdict"] == "pass"

    # No ATA create: the band collapses to the slack on either side.
    exists_tx = build_swap_tx(include_wrap_primitives=False).tx_b64
    exists = await _band(
        exists_tx,
        sol_after=_SOL_BEFORE - _LAMPORTS - unconditional,
        workdir=tmp_path / "exists",
    )
    low, high = exists["balance"]["sol_spent_explained_range"]
    assert (low, high) == (50_100_200, 50_110_200)
    assert high - low == 2 * _BASE_FEE  # width is the rent (zero) + slack
    assert exists["verdict"] == "pass"


async def test_a_spend_with_no_fees_paid_is_not_explained(tmp_path, monkeypatch):
    """The defect the old floor admitted: swap amount, zero fees, `pass`."""
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        _mock_post_approval_rpc(m)
        m.post(_SUBMIT_RE, payload=_rpc(_expected_signature(tx)))
        # Exactly the swap amount left the wallet and nothing else, which
        # no real transaction can do.
        _mock_settlement_rpc(m, sol_after=_SOL_BEFORE - _LAMPORTS)
        assert await runner.place(sol=_SOL) == EXIT_REVIEW
    record = _step(_steps(tmp_path), "reconciliation")
    assert record["verdict"] == "review"
    assert any("outside the explained range" in m for m in record["mismatches"])
    await session.close()
    await db.close()


async def test_reconciliation_reviews_an_unexplained_balance_move(
    tmp_path, monkeypatch
):
    """Receiving less than the on-chain minimum is money we cannot account for."""
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        _mock_post_approval_rpc(m)
        m.post(_SUBMIT_RE, payload=_rpc(_expected_signature(tx)))
        _mock_settlement_rpc(m, usdc_after=_MIN_OUT_RAW - 1)
        assert await runner.place(sol=_SOL) == EXIT_REVIEW

    reconciliation = _step(_steps(tmp_path), "reconciliation")
    assert reconciliation["verdict"] == "review"
    assert reconciliation["balance"]["meets_minimum_output"] is False
    assert any(
        "the slippage bound did not hold" in m for m in reconciliation["mismatches"]
    )
    await session.close()
    await db.close()


# ======================================================================
# Confirmation classification
# ======================================================================
async def test_a_transaction_seen_landed_is_never_reported_as_never_seen(tmp_path):
    """Once the cluster acknowledges the signature, silence is not absence.

    Calling a later run of failed polls a "confirmation timeout" would send
    the caller off to resolve a transaction we already watched land, and would
    read in the evidence as "never seen" when the opposite was observed.
    """
    from scout.live.solana.rpc_client import SignatureStatus

    runner, db, session = await _make_runner(tmp_path)
    answers = [
        [SignatureStatus(known=True, slot=7, err=None, confirmation_status="confirmed")]
    ]

    async def _statuses(_sigs, **_kw):
        if answers:
            return answers.pop(0)
        raise TimeoutError("node stopped answering")

    runner._rpc.get_signature_statuses = _statuses
    result = await runner._await_confirmation("5sig")
    assert result["outcome"] == "finalize_timeout"
    assert result["known"] is False  # the LAST poll failed...
    assert result["poll_errors"]  # ...and that is recorded, not hidden
    await session.close()
    await db.close()


async def test_a_signature_the_cluster_never_knew_is_a_confirm_timeout(tmp_path):
    from scout.live.solana.rpc_client import SignatureStatus

    runner, db, session = await _make_runner(tmp_path)

    async def _statuses(_sigs, **_kw):
        return [SignatureStatus(known=False)]

    runner._rpc.get_signature_statuses = _statuses
    result = await runner._await_confirmation("5sig")
    assert result["outcome"] == "confirm_timeout"
    await session.close()
    await db.close()


# ======================================================================
# Ambiguous submission — all four verdicts
# ======================================================================
async def _run_ambiguous(tmp_path, monkeypatch, *, resolver_payloads):
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _expected_signature(tx)[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        _mock_post_approval_rpc(m)
        m.post(_SUBMIT_RE, exception=TimeoutError())
        for payload in resolver_payloads:
            if isinstance(payload, dict):
                m.post(_RPC_URL, payload=payload)
            else:
                payload(m)
        code = await runner.place(sol=_SOL)
        submissions = _submissions(m)
    return code, db, session, tx, submissions


async def test_ambiguous_then_landed_is_adopted_not_rebuilt(tmp_path, monkeypatch):
    code, db, session, tx, submissions = await _run_ambiguous(
        tmp_path,
        monkeypatch,
        resolver_payloads=[
            _sig_status(status="confirmed"),  # resolver sweep 1
            # balances the resolver records as supplementary evidence
            _rpc({"context": {"slot": 1}, "value": _SOL_AFTER}),
            _token_accounts(0),
            _token_accounts(_OUT_RAW),
            _sig_status(),  # confirmation poll
            _rpc({"slot": 283_000_455, "meta": {"fee": _TX_FEES, "err": None}}),
            _rpc({"context": {"slot": 1}, "value": _SOL_AFTER}),
            _token_accounts(_OUT_RAW),
        ],
    )
    assert code == EXIT_OK
    # Exactly ONE POST to the block engine, ever. No resend, no rebuild.
    assert len(submissions) == 1
    steps = _steps(tmp_path)
    assert _step(steps, "ambiguity_resolution")["verdict"] == "landed"
    assert _step(steps, "ambiguity_adopted") is not None
    assert (await _live_rows(db))[0][0] == "open"
    await session.close()
    await db.close()


async def test_ambiguous_then_failed_on_chain_rejects_the_row(tmp_path, monkeypatch):
    code, db, session, tx, submissions = await _run_ambiguous(
        tmp_path,
        monkeypatch,
        resolver_payloads=[
            _sig_status(err={"InstructionError": [4, "Custom"]}),
            _rpc({"context": {"slot": 1}, "value": _SOL_AFTER}),
            _token_accounts(0),
            _token_accounts(0),
        ],
    )
    assert code == EXIT_REFUSED
    assert len(submissions) == 1
    steps = _steps(tmp_path)
    assert _step(steps, "ambiguity_resolution")["verdict"] == "failed_on_chain"
    assert _step(steps, "ambiguity_failed_on_chain")["ledger_status"] == "rejected"
    assert (await _live_rows(db))[0][0] == "rejected"
    await session.close()
    await db.close()


async def test_ambiguous_then_definitively_not_submitted_clears_the_lane(
    tmp_path, monkeypatch, capsys
):
    """Absent AND expired: the only verdict that says a rerun is safe."""
    expired = _rpc(_LAST_VALID + 50)
    code, db, session, tx, submissions = await _run_ambiguous(
        tmp_path,
        monkeypatch,
        resolver_payloads=[
            _sig_status(known=False),  # sweep 1: absent
            expired,  # sweep 1: block height, past lastValidBlockHeight
            _sig_status(known=False),  # sweep 2: absent
            expired,  # sweep 2: still expired
            _rpc({"context": {"slot": 1}, "value": _SOL_BEFORE}),
            _token_accounts(0),
            _token_accounts(0),
        ],
    )
    assert code == EXIT_REFUSED
    assert len(submissions) == 1
    steps = _steps(tmp_path)
    resolution = _step(steps, "ambiguity_resolution")
    assert resolution["verdict"] == "definitively_not_submitted"
    assert resolution["rebuild_is_safe"] is True
    assert (await _live_rows(db))[0][0] == "rejected"
    assert "SUBMISSION DID NOT LAND" in capsys.readouterr().out
    await session.close()
    await db.close()


async def test_ambiguous_then_unresolved_escalates_and_blocks_the_next_run(
    tmp_path, monkeypatch, capsys
):
    """The dangerous verdict: stop, do not rebuild, and block the lane."""
    code, db, session, tx, submissions = await _run_ambiguous(
        tmp_path,
        monkeypatch,
        resolver_payloads=[
            _sig_status(known=False),
            _rpc(_HEIGHT_FRESH),  # still landable — absence proves nothing
            _rpc({"context": {"slot": 1}, "value": _SOL_BEFORE}),
            _token_accounts(0),
            _token_accounts(0),
        ],
    )
    assert code == EXIT_ESCALATE
    assert len(submissions) == 1
    steps = _steps(tmp_path)
    assert (
        _step(steps, "ambiguity_unresolved")["ledger_status"] == "needs_manual_review"
    )
    out = capsys.readouterr().out
    assert "DO NOT REBUILD" in out
    assert (await _live_rows(db))[0][0] == "needs_manual_review"

    decision_id = _step(steps, "run_started")["decision_id"]
    signature = _expected_signature(tx)
    await session.close()

    # A FRESH runner against the same database refuses to place anything.
    runner2, db2, session2 = await _make_runner(tmp_path)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_HEIGHT_FRESH))
        assert await runner2.place(sol=_SOL) == EXIT_BLOCKED
        assert _submissions(m) == []
        assert _posts_to(m, _QUOTE_RE) == []  # never even quoted
    assert "SOLANA PILOT LANE BLOCKED" in capsys.readouterr().out
    # The blocked run resolved the row using the signature and the height it
    # read back out of the first run's evidence file.
    blocked_steps = [
        json.loads(line)
        for line in _evidence_path(tmp_path, decision_id)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert any(s["step"] == "ambiguity_unresolved" for s in blocked_steps)
    assert signature  # the identity the whole recovery hangs on
    await session2.close()
    await db2.close()
    await db.close()


# ======================================================================
# Startup reconciliation
# ======================================================================
async def test_startup_auto_retires_a_row_whose_transaction_can_never_land(
    tmp_path, monkeypatch
):
    """definitively_not_submitted is the one verdict that clears the lane."""
    tx = build_swap_tx().tx_b64
    signature = _expected_signature(tx)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a06876"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(db, decision_id=decision_id, signature=signature)
    _write_intent_evidence(
        tmp_path, decision_id, signature=signature, last_valid=_LAST_VALID
    )
    _authorize(monkeypatch, None)  # stop at the approval boundary

    with aioresponses() as m:
        # Startup reconciliation: two absent sweeps past the expiry height.
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        # The lane cleared, so the run proceeded all the way to the prompt.
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    old = await _row(db, decision_id)
    assert old["status"] == "rejected"
    await session.close()
    await db.close()


async def test_startup_keeps_blocking_a_row_whose_swap_landed(tmp_path):
    """A landed swap left a position; retiring the row would deny it exists."""
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a06877"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(db, decision_id=decision_id, signature=signature)
    _write_intent_evidence(
        tmp_path, decision_id, signature=signature, last_valid=_LAST_VALID
    )
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_sig_status())
        assert await runner.place(sol=_SOL) == EXIT_BLOCKED
        assert _submissions(m) == []
        assert _posts_to(m, _QUOTE_RE) == []
    assert (await _row(db, decision_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_a_missing_evidence_file_makes_the_row_block(tmp_path):
    """No lastValidBlockHeight means expiry is unprovable — fail closed."""
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a06878"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(db, decision_id=decision_id, signature=signature)
    # Deliberately NO intent evidence written.
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        assert await runner.place(sol=_SOL) == EXIT_BLOCKED
        assert _submissions(m) == []
    row = await _row(db, decision_id)
    assert row["status"] == "open"
    await session.close()
    await db.close()


async def test_a_row_without_a_signature_blocks_rather_than_being_repaired(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(
        db, decision_id="6d1b345e-2821-40e2-ad83-4ecb18a06879", signature=None
    )
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_BLOCKED
        assert _submissions(m) == []
        assert list(m.requests) == []
    await session.close()
    await db.close()


# ======================================================================
# Age-derived blockhash expiry — bounded on both sides
# ======================================================================
def _report(verdict, *, outcomes=("absent",), lvbh=None):
    from scout.live.solana.resolver import ResolutionProbe, ResolutionReport

    return ResolutionReport(
        verdict=verdict,
        signature="5sig",
        last_valid_block_height=lvbh,
        probes=tuple(
            ResolutionProbe(sweep=i, outcome=o) for i, o in enumerate(outcomes)
        ),
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


def test_row_age_refuses_to_guess(tmp_path):
    """An age that cannot be established must not license retiring a row."""
    now = datetime.now(timezone.utc)
    assert solana_pilot.row_age_seconds(None) is None
    assert solana_pilot.row_age_seconds("") is None
    assert solana_pilot.row_age_seconds("not-a-timestamp") is None
    # Naive timestamps are of unknown origin — every writer here stamps UTC.
    assert solana_pilot.row_age_seconds("2026-07-31T12:00:00") is None
    aged = (now - timedelta(hours=2)).isoformat()
    assert solana_pilot.row_age_seconds(aged, now=now) == pytest.approx(7200, abs=2)


def test_age_policy_regimes(tmp_path):
    """The three regimes, and the two that must never clear a row."""
    settings = _settings(tmp_path)
    absent = _report("unresolved")

    # Too young: age proves nothing, so the resolver's answer stands.
    young = solana_pilot.apply_age_policy(
        absent, age_seconds=600, settings=settings, had_evidence_height=False
    )
    assert young.verdict == "unresolved"
    assert young.expiry_source is None

    # Inside the window: age alone establishes expiry.
    inside = solana_pilot.apply_age_policy(
        absent, age_seconds=7200, settings=settings, had_evidence_height=False
    )
    assert inside.verdict == "definitively_not_submitted"
    assert inside.expiry_source == "row_age"
    assert inside.clears_the_lane is True

    # Past the history window: absence stops being evidence.
    stale = solana_pilot.apply_age_policy(
        absent, age_seconds=200_000, settings=settings, had_evidence_height=False
    )
    assert stale.verdict == "unresolved"
    assert stale.reason == "history_window_exceeded"
    assert stale.clears_the_lane is False

    # ...even when the evidence file DID prove expiry. A landed transaction
    # past the history window reads as absent, and that is the one outcome
    # the resolver exists to prevent.
    proven = _report("definitively_not_submitted", lvbh=_LAST_VALID)
    assert (
        solana_pilot.apply_age_policy(
            proven, age_seconds=200_000, settings=settings, had_evidence_height=True
        ).reason
        == "history_window_exceeded"
    )

    # A recorded height is the fresher, more specific fact; age must not
    # overrule it when it says the transaction can still land.
    assert (
        solana_pilot.apply_age_policy(
            absent, age_seconds=7200, settings=settings, had_evidence_height=True
        ).verdict
        == "unresolved"
    )

    # Presence is trustworthy at any age — the window bounds what ABSENCE
    # means, not what presence means.
    for verdict in ("landed", "failed_on_chain"):
        carried = solana_pilot.apply_age_policy(
            _report(verdict, outcomes=(verdict,)),
            age_seconds=200_000,
            settings=settings,
            had_evidence_height=False,
        )
        assert carried.verdict == verdict
        assert carried.reason is None

    # A probe that ERRORED is "we could not look", not "it is not there".
    errored = _report("unresolved", outcomes=("error",))
    assert (
        solana_pilot.apply_age_policy(
            errored, age_seconds=7200, settings=settings, had_evidence_height=False
        ).verdict
        == "unresolved"
    )


async def test_a_row_inside_the_window_resolves_without_its_evidence_file(
    tmp_path, capsys
):
    """*** The case age-based expiry exists for. ***

    The evidence file is gone, so lastValidBlockHeight is unavailable and the
    resolver alone can only say `unresolved` — which would block the lane for
    good. `created_at` is on the row, a blockhash lives at most 150 slots, so
    a two-hour-old row has provably expired whatever height it carried.
    """
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a0688b"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(
        db, decision_id=decision_id, signature=signature, age_seconds=7200
    )
    # Deliberately NO intent evidence written — this is the pruned-directory
    # case, and it must still resolve.
    assert (
        solana_pilot.read_persisted_intent(tmp_path / "evidence", decision_id) is None
    )

    with aioresponses() as m:
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_HEIGHT_FRESH))
        assert await runner.resolve(decision_id=decision_id) == EXIT_OK

    record = _step(_steps(tmp_path), "resolution")
    assert record["verdict"] == "unresolved"  # what the cluster alone could say
    assert record["lane_verdict"] == "definitively_not_submitted"
    assert record["expiry_source"] == "row_age"
    assert record["last_valid_block_height_source"] == "unavailable"
    assert (await _row(db, decision_id))["status"] == "rejected"
    assert "definitively_not_submitted" in capsys.readouterr().out
    await session.close()
    await db.close()


async def test_a_row_below_the_lower_bound_falls_back_to_the_evidence_file(tmp_path):
    """Too young for age to prove anything — the recorded height decides."""
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a0688c"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(
        db, decision_id=decision_id, signature=signature, age_seconds=600
    )
    _write_intent_evidence(
        tmp_path, decision_id, signature=signature, last_valid=_LAST_VALID
    )
    with aioresponses() as m:
        for _ in range(2):
            m.post(_RPC_URL, payload=_sig_status(known=False))
            m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        assert await runner.resolve(decision_id=decision_id) == EXIT_OK

    record = _step(_steps(tmp_path), "resolution")
    assert record["expiry_source"] == "evidence_last_valid_block_height"
    assert record["lane_verdict"] == "definitively_not_submitted"
    assert (await _row(db, decision_id))["status"] == "rejected"
    await session.close()
    await db.close()


async def test_a_young_row_without_an_evidence_file_still_blocks(tmp_path):
    """Neither source can establish expiry, so nothing is retired."""
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a0688d"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(
        db, decision_id=decision_id, signature=signature, age_seconds=600
    )
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_HEIGHT_FRESH))
        assert await runner.resolve(decision_id=decision_id) == EXIT_ESCALATE
    record = _step(_steps(tmp_path), "resolution")
    assert record["lane_verdict"] == "unresolved"
    assert record["expiry_source"] is None
    assert (await _row(db, decision_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_past_the_history_window_absence_is_no_longer_evidence(tmp_path, capsys):
    """*** The bound that makes age-based expiry safe. ***

    searchTransactionHistory only reaches as far back as the node keeps
    ledger history. Past that, a swap that LANDED reads exactly like one that
    never existed — so retiring the row here would be the precise failure the
    resolver exists to prevent.
    """
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a0688e"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(
        db, decision_id=decision_id, signature=signature, age_seconds=200_000
    )
    # Even WITH a recorded height that proves expiry, the row must not clear.
    _write_intent_evidence(
        tmp_path, decision_id, signature=signature, last_valid=_LAST_VALID
    )
    with aioresponses() as m:
        for _ in range(2):
            m.post(_RPC_URL, payload=_sig_status(known=False))
            m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        assert await runner.resolve(decision_id=decision_id) == EXIT_ESCALATE

    record = _step(_steps(tmp_path), "resolution")
    assert record["verdict"] == "definitively_not_submitted"  # cluster's view
    assert record["lane_verdict"] == "unresolved"  # what we may act on
    assert record["age_policy_reason"] == "history_window_exceeded"
    assert _step(_steps(tmp_path), "row_auto_retired") is None
    assert (await _row(db, decision_id))["status"] == "open"
    out = capsys.readouterr().out
    assert "history-history" not in out  # sanity: no duplicated phrasing
    assert "older than the node's transaction-history" in out
    await session.close()
    await db.close()


async def test_age_derived_expiry_still_requires_a_pinned_endpoint(tmp_path):
    """Age establishes expiry; it does not make a load-balanced read safe."""
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a0688f"
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_RPC_URL=_ROUND_ROBIN_RPC_URL
    )
    await _seed_solana_row(
        db, decision_id=decision_id, signature=signature, age_seconds=7200
    )
    with aioresponses() as m:
        m.post(_ROUND_ROBIN_RPC_URL, payload=_sig_status(known=False))
        m.post(_ROUND_ROBIN_RPC_URL, payload=_rpc(_HEIGHT_FRESH))
        assert await runner.resolve(decision_id=decision_id) == EXIT_OK

    steps = _steps(tmp_path)
    assert _step(steps, "resolution")["lane_verdict"] == "definitively_not_submitted"
    assert _step(steps, "resolution")["expiry_source"] == "row_age"
    assert _step(steps, "auto_retire_withheld") is not None
    assert _step(steps, "row_auto_retired") is None
    assert (await _row(db, decision_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_startup_reconciliation_clears_an_aged_row_without_evidence(
    tmp_path, monkeypatch
):
    """The lane unblocks itself on the next `place`, not just via `resolve`."""
    tx = build_swap_tx().tx_b64
    stale_sig = _expected_signature(build_swap_tx(tip_lamports=98_765).tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a06890"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(
        db, decision_id=decision_id, signature=stale_sig, age_seconds=7200
    )
    _authorize(monkeypatch, None)  # stop at the approval boundary

    with aioresponses() as m:
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_HEIGHT_FRESH))
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        # Lane cleared, so the run reached the approval prompt and stopped
        # there on a closed stdin.
        assert await runner.place(sol=_SOL) == EXIT_REFUSED

    reconciliation = _step(_steps(tmp_path), "startup_reconciliation")
    assert reconciliation["blocker_count"] == 0
    row = reconciliation["rows"][0]
    assert row["resolution"]["expiry_source"] == "row_age"
    assert row["auto_retired"] is True
    assert (await _row(db, decision_id))["status"] == "rejected"
    await session.close()
    await db.close()


# ======================================================================
# resolve
# ======================================================================
async def test_resolve_reports_and_auto_retires_only_when_it_can_never_land(
    tmp_path, capsys
):
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a0687a"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(
        db, decision_id=decision_id, signature=signature, status="needs_manual_review"
    )
    _write_intent_evidence(
        tmp_path, decision_id, signature=signature, last_valid=_LAST_VALID
    )
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        assert await runner.resolve(decision_id=decision_id) == EXIT_OK
        assert _submissions(m) == []
    assert (await _row(db, decision_id))["status"] == "rejected"
    assert "definitively_not_submitted" in capsys.readouterr().out
    await session.close()
    await db.close()


async def test_resolve_leaves_a_landed_row_alone(tmp_path):
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a0687b"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(db, decision_id=decision_id, signature=signature)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_sig_status())
        assert await runner.resolve(decision_id=decision_id) == EXIT_BLOCKED
    assert (await _row(db, decision_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_resolve_refuses_an_unknown_decision_id(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    assert (
        await runner.resolve(decision_id="6d1b345e-2821-40e2-ad83-4ecb18a0687c")
        == EXIT_REFUSED
    )
    await session.close()
    await db.close()


# ======================================================================
# status
# ======================================================================
async def test_status_is_read_only_and_reports_blockers(tmp_path, capsys):
    signature = _expected_signature(build_swap_tx().tx_b64)
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a0687d"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(db, decision_id=decision_id, signature=signature)
    _write_intent_evidence(
        tmp_path, decision_id, signature=signature, last_valid=_LAST_VALID
    )
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": _SOL_BEFORE}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        # The row resolves as never-submitted, which `place` would auto-retire.
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        assert await runner.status() == EXIT_OK
        assert _submissions(m) == []
        assert _posts_to(m, _SWAP_URL) == []

    out = capsys.readouterr().out
    assert "SOLANA PILOT STATUS" in out
    assert "keypair custody          : ok" in out
    assert "place would auto-retire this" in out
    # Read-only means read-only: status did NOT retire it.
    assert (await _row(db, decision_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_status_reports_a_refused_keypair_without_crashing(tmp_path, capsys):
    runner, db, session = await _make_runner(tmp_path, keypair=None)
    with aioresponses():
        assert await runner.status() == EXIT_OK
    out = capsys.readouterr().out
    assert "keypair custody          : REFUSED" in out
    await session.close()
    await db.close()


# ======================================================================
# Evidence
# ======================================================================
async def test_evidence_is_jsonlines_carries_the_signature_and_leaks_no_key(
    tmp_path, monkeypatch
):
    tx = build_swap_tx().tx_b64
    signature = _expected_signature(tx)
    _authorize(monkeypatch, signature[:8])
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_happy_path(m, tx)
        assert await runner.place(sol=_SOL) == EXIT_OK

    raw = _only_evidence_file(tmp_path).read_text(encoding="utf-8")
    steps = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(steps) > 15
    assert all("step" in s and "at" in s for s in steps)
    for step in steps:
        assert datetime.fromisoformat(step["at"]).tzinfo is not None

    # The signature is present, and present BEFORE the submission record.
    names = [s["step"] for s in steps]
    assert names.index("signed_in_memory") < names.index("submitted")
    assert signature in raw

    # No key MATERIAL anywhere — the secret bytes are never handled by this
    # module at all. The key FILE's path is recorded on purpose: it is not a
    # secret, and an evidence pack that cannot say which key signed has lost
    # provenance for nothing.
    assert _PLANTED_KEY_BYTES not in raw
    custody = _step(steps, "keypair_custody")
    assert custody["signing_key_file"] == _PLANTED_KEYPAIR_PATH
    assert custody["signer_pubkey"] == PAYER_PUBKEY
    await session.close()
    await db.close()


async def test_evidence_survives_a_crash_mid_run(tmp_path):
    """Every completed step is fsynced before the next one starts."""
    runner, db, session = await _make_runner(tmp_path)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash after the quote")

    with aioresponses() as m:
        m.get(_QUOTE_RE, payload=_quote_payload())
        runner._jupiter.build_swap_transaction = _boom
        assert await runner.place(sol=_SOL) == EXIT_ESCALATE
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    names = _step_names(steps)
    assert "envelope_gate" in names and "quote" in names
    assert _step(steps, "unexpected_error")["error_type"] == "RuntimeError"
    await session.close()
    await db.close()


def test_evidence_scrubs_secret_shaped_keys():
    scrubbed = solana_pilot._scrub(
        {
            "keypair_path": "/srv/id.json",
            "API-Key": "xyz",
            "nested": {"private_key": "s3cr3t"},
            "signal_type": "solana_pilot",
            "expected_signature": "5xY",
            "signer_pubkey": "abc",
            "raw": b"\x00\x01\x02",
        }
    )
    assert scrubbed["keypair_path"] == "[REDACTED]"
    assert scrubbed["API-Key"] == "[REDACTED]"
    assert scrubbed["nested"]["private_key"] == "[REDACTED]"
    # Public identifiers must survive — they are the whole audit trail.
    assert scrubbed["signal_type"] == "solana_pilot"
    assert scrubbed["expected_signature"] == "5xY"
    assert scrubbed["signer_pubkey"] == "abc"
    assert scrubbed["raw"] == "<3 bytes>"


def test_persisted_intent_reads_back_and_fails_closed(tmp_path):
    path = tmp_path / "evidence"
    assert solana_pilot.read_persisted_intent(path, "nope") is None
    path.mkdir()
    target = path / "solana_pilot_abc.json"
    target.write_text('{"step": "run_started"}\nnot json\n', encoding="utf-8")
    assert solana_pilot.read_persisted_intent(path, "abc") is None
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"step": "intent_persisted", "last_valid_block_height": 42})
            + "\n"
        )
    assert (
        solana_pilot.read_persisted_intent(path, "abc")["last_valid_block_height"] == 42
    )


# ======================================================================
# Ledger anchor
# ======================================================================
async def test_pilot_anchor_is_created_once_and_hidden_from_paper_readers(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    first = await solana_pilot.ensure_pilot_anchor(db)
    second = await solana_pilot.ensure_pilot_anchor(db)
    assert first == second
    assert await _count(db, "paper_trades") == 1

    # Not 'open', so no open-position loop scans it.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
    )
    assert (await cur.fetchone())[0] == 0
    # Epoch opened_at keeps it out of every "today" / recent-window query.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE date(opened_at) = date('now')"
    )
    assert (await cur.fetchone())[0] == 0
    cur = await db._conn.execute(
        "SELECT closed_at FROM paper_trades WHERE id = ?", (first,)
    )
    assert (await cur.fetchone())[0] is None
    await session.close()
    await db.close()


async def test_the_intent_insert_writes_the_signature_atomically(tmp_path):
    """One statement, so no crash window leaves an unresolvable 'open' row."""
    runner, db, session = await _make_runner(tmp_path)
    anchor = await solana_pilot.ensure_pilot_anchor(db)
    row_id = await solana_pilot.record_solana_intent(
        db,
        decision_id="6d1b345e-2821-40e2-ad83-4ecb18a0687e",
        expected_signature="5sig",
        paper_trade_id=anchor,
        size_usd="8.52",
        sol_price_usdc="170.5",
    )
    assert row_id > 0
    row = await _row(db, "6d1b345e-2821-40e2-ad83-4ecb18a0687e")
    assert (row["status"], row["entry_order_id"]) == ("open", "5sig")
    await session.close()
    await db.close()


# ======================================================================
# Lock + main() wiring
# ======================================================================
def test_pilot_lock_is_exclusive_and_never_broken_automatically(tmp_path):
    import os

    db_path = tmp_path / "pilot.db"
    fd, path = solana_pilot.acquire_pilot_lock(db_path)
    try:
        with pytest.raises(PilotLockHeld) as excinfo:
            solana_pilot.acquire_pilot_lock(db_path)
        # The holder's PID is named so the operator knows what to look for;
        # the lock is never broken for them.
        assert str(os.getpid()) in excinfo.value.holder
        assert path.exists()
    finally:
        solana_pilot.release_pilot_lock(fd, path)
    assert not path.exists()


def test_the_solana_lock_is_not_the_kraken_lock(tmp_path):
    """Two lanes, two assets, two venues — neither bounds the other."""
    from scout.live import kraken_pilot

    db_path = tmp_path / "pilot.db"
    assert solana_pilot.pilot_lock_path(db_path) != kraken_pilot.pilot_lock_path(
        db_path
    )


async def test_main_refuses_when_the_database_does_not_exist(
    tmp_path, monkeypatch, capsys
):
    """sqlite3 creates a DB for any path, so the wrong directory is silent."""
    missing = tmp_path / "nowhere" / "scout.db"
    monkeypatch.setattr(
        solana_pilot, "load_settings", lambda: _settings(tmp_path, DB_PATH=str(missing))
    )
    assert await solana_pilot.main(["status"]) == EXIT_REFUSED
    assert str(missing.resolve()) in capsys.readouterr().out
    assert not missing.exists()


async def test_main_blocks_place_but_not_status_while_the_lock_is_held(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "pilot.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()
    settings = _settings(tmp_path, DB_PATH=str(db_path))
    monkeypatch.setattr(solana_pilot, "load_settings", lambda: settings)
    solana_pilot.pilot_lock_path(db_path).write_text("pid=4242 acquired_at=now\n")

    with aioresponses() as m:
        assert await solana_pilot.main(["place", "--sol", "0.05"]) == EXIT_BLOCKED
        assert list(m.requests) == []  # refused before any venue was touched
    out = capsys.readouterr().out
    assert "4242" in out and "Two runs are two swaps" in out

    # status still works — it is how the operator investigates the stale lock.
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": 1}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        assert await solana_pilot.main(["status"]) == EXIT_OK
    assert solana_pilot.pilot_lock_path(db_path).exists()  # untouched
    solana_pilot.pilot_lock_path(db_path).unlink()


async def test_main_releases_the_lock_after_a_run(tmp_path, monkeypatch):
    db_path = tmp_path / "pilot.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()
    settings = _settings(tmp_path, DB_PATH=str(db_path), SOLANA_PILOT_ENABLED=False)
    monkeypatch.setattr(solana_pilot, "load_settings", lambda: settings)
    with aioresponses():
        assert await solana_pilot.main(["place", "--sol", "0.05"]) == EXIT_REFUSED
    assert not solana_pilot.pilot_lock_path(db_path).exists()


# ======================================================================
# CLI argument handling
# ======================================================================
@pytest.mark.parametrize("bad", ["x", "NaN", "Infinity", "0", "-1", "0.0000000001"])
def test_sol_argument_rejects_what_would_reach_the_trading_logic(bad):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["place", "--sol", bad])


def test_sol_argument_accepts_a_whole_number_of_lamports():
    args = build_parser().parse_args(["place", "--sol", "0.05"])
    assert args.sol == Decimal("0.05")
    assert solana_pilot.lamports_from_sol(args.sol) == _LAMPORTS


def test_decision_id_is_stripped_and_shape_checked():
    args = build_parser().parse_args(
        ["resolve", "--decision-id", "  6d1b345e-2821-40e2-ad83-4ecb18a06876 "]
    )
    assert args.decision_id == "6d1b345e-2821-40e2-ad83-4ecb18a06876"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["resolve", "--decision-id", "nope"])


def test_there_is_no_cancel_subcommand():
    """Solana has no cancellation primitive; offering one would be a lie."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["cancel", "--decision-id", "x"])


async def test_operator_lines_are_ascii(tmp_path, monkeypatch, capsys):
    """The approval block is read on whatever console the operator has."""
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, "nope")
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        await runner.place(sol=_SOL)
    out = capsys.readouterr().out
    assert "→" not in out  # no rightwards arrow anywhere
    numeric = [
        line
        for line in out.splitlines()
        if any(
            token in line
            for token in (
                "exact input",
                "expected output",
                "minimum acceptable",
                "price impact",
                "jito tip",
                "max total fee",
                "current SOL",
                "current USDC",
                "blockhash age",
            )
        )
    ]
    assert numeric
    for line in numeric:
        assert line.isascii(), f"non-ascii in a numeric line: {line!r}"
    assert "  pair                : SOL -> USDC" in out
    await session.close()
    await db.close()


# ======================================================================
# Config
# ======================================================================
def test_the_total_fee_ceiling_admits_a_first_ever_swap():
    """The ruling this default exists to encode.

    A first SOL->USDC swap creates the wSOL account and the USDC account.
    At 2,039,280 lamports of rent each that is 4,078,560 before a single
    fee — so a ceiling set for fees alone refuses the legitimate build.
    """
    s = Settings(_env_file=None, **_REQUIRED)
    assert s.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS == 8_500_000
    assert s.SOLANA_PILOT_MAX_ATA_CREATES == 3
    worst_case = (
        3 * 2_039_280
        + s.SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS
        + s.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS
        + 5_000
    )
    assert worst_case == 8_122_840
    assert s.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS >= worst_case


def test_config_rejects_a_ceiling_no_build_could_ever_satisfy():
    """A ceiling below the computable floor is a lane that never trades.

    The old floor knew nothing about ATA rent, so it accepted ceilings that
    tx_inspector then refused on every single transaction — a config error
    that only showed up as "the pilot does not work" on trade day.
    """
    with pytest.raises(ValueError) as excinfo:
        Settings(
            _env_file=None,
            **_REQUIRED,
            SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=2_500_000,
        )
    message = str(excinfo.value)
    # The arithmetic is named, so an operator who tightened the ceiling can
    # see exactly which term made it impossible.
    assert "required>=8122840" in message
    assert "ATA rent 6117840" in message
    assert "3 accounts x 2039280" in message

    # Lowering the ATA allowance lowers the floor with it.
    tighter = Settings(
        _env_file=None,
        **_REQUIRED,
        SOLANA_PILOT_MAX_ATA_CREATES=0,
        SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=2_005_000,
    )
    assert tighter.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS == 2_005_000


def test_solana_pilot_defaults_are_safe():
    s = Settings(_env_file=None, **_REQUIRED)
    assert s.SOLANA_PILOT_ENABLED is False
    assert s.SOLANA_PILOT_KEYPAIR_PATH == ""
    assert s.SOLANA_RESOLVER_RPC_URL == ""
    assert s.SOLANA_PILOT_MIN_ORDER_USD == 5.0
    assert s.SOLANA_PILOT_MAX_ORDER_USD == 10.0
    assert s.SOLANA_PILOT_JITO_TIP_LAMPORTS == 100_000
    assert s.SOLANA_PILOT_MAX_PRICE_IMPACT_PCT == 1.0
    assert s.SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS == 15


@pytest.mark.parametrize(
    "overrides,fragment",
    [
        (dict(SOLANA_PILOT_JITO_TIP_LAMPORTS=2_000_000), "MAX_JITO_TIP"),
        (dict(SOLANA_PILOT_JITO_TIP_LAMPORTS=999), "minimum tip is 1000"),
        (dict(SOLANA_PILOT_MAX_PRICE_IMPACT_PCT=0), "in (0, 100]"),
        (dict(SOLANA_PILOT_BLOCKHASH_SAFETY_MARGIN_BLOCKS=-1), "must be >= 0"),
    ],
)
def test_runner_config_rejects_an_envelope_that_could_never_trade(overrides, fragment):
    with pytest.raises(ValueError, match=re.escape(fragment)):
        Settings(_env_file=None, **_REQUIRED, **overrides)
