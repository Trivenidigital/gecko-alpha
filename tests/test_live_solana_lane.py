"""PR-S2: the Solana DEX execution lane runner.

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
from scout.live import solana_lane
from scout.live.kill_switch import KillSwitch
from scout.live.solana.constants import (
    SOL_MINT,
    SOLANA_MAINNET_GENESIS_HASH,
    USDC_MINT,
)
from scout.live.solana.jito_client import JitoClient
from scout.live.solana.jupiter_client import JupiterClient
from scout.live.solana.resolver_pool import ResolverPool, redact_endpoint
from scout.live.solana.rpc_client import SolanaRpcClient
from scout.live.solana.signer import (
    REQUIRED_MODE,
    SolanaKeypairError,
    enforce_keyfile_security,
    sign_transaction,
)
from scout.live.solana_lane import (
    EXIT_BLOCKED,
    EXIT_ESCALATE,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_REVIEW,
    LaneLockHeld,
    LaneRunner,
    build_parser,
)
from solana_tx_builder import OTHER, PAYER, PAYER_PUBKEY, STRANGER, build_swap_tx

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
_RPC_URL = "https://dedicated-node.example-rpc.net/rpc"
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
# Not mainnet-beta. Every endpoint in the resolver pool has to prove which
# chain it serves before it is allowed to answer a question about a signature.
_DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
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
    """Stands in for `signer.load_keypair`, and COUNTS being called.

    The count is the point. "The funded key was never read on this path" is a
    claim about an absence, and the only way to assert an absence is to make
    the thing being absent observable.
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
    """Stands in for the stat-only custody probe.

    Injected because the real one refuses on Windows by design — it cannot
    verify POSIX mode or ownership there — and the test suite runs on
    developer machines as well as the deployment host.
    """

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
    """Runner + db + session against a tmp DB. Caller closes db and session.

    The loader and the custody probe are separate injections because they are
    separate in the runner: custody is checked from stat before anything is
    built, the key is read only after the operator authorizes. Both spies are
    attached to the runner so a test can assert on call counts.
    """
    settings = _settings(tmp_path, **overrides)
    db = Database(tmp_path / "lane.db")
    await db.initialize()
    session = aiohttp.ClientSession()
    loader = _LoaderSpy(keypair)
    prober = _CustodyProbeSpy(ok=custody_ok)
    # The resolver pool is built from settings exactly as `main()` builds it,
    # so a test that configures SOLANA_RESOLVER_RPC_URLS gets the pool the
    # deployment would get rather than a one-endpoint stand-in.
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


def _authorize(monkeypatch, typed: str | None = None) -> None:
    """Feed the approval prompt. ``None`` raises EOF (non-interactive stdin)."""

    def _fake_input(_prompt: str = "") -> str:
        if typed is None:
            raise EOFError
        return typed

    monkeypatch.setattr("builtins.input", _fake_input)


def _expected_signature(tx_b64: str) -> str:
    """The signature the runner will derive for this transaction AFTER approval."""
    return sign_transaction(tx_b64, PAYER, expected_signer=PAYER_PUBKEY).signature


def _message_sha256(tx_b64: str) -> str:
    """The hash the operator authorizes — computed WITHOUT any key.

    Deliberately derived here from the transaction bytes rather than read out
    of the runner, so the test and the runner agree only if both are hashing
    the same thing.
    """
    import base64
    import hashlib

    from solders.message import to_bytes_versioned
    from solders.transaction import VersionedTransaction

    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    return hashlib.sha256(to_bytes_versioned(tx.message)).hexdigest()


def _phrase(tx_b64: str) -> str:
    """What the operator types: the first 8 chars of the message hash."""
    return _message_sha256(tx_b64)[:8]


def _evidence_path(tmp_path, decision_id: str):
    return tmp_path / "evidence" / f"solana_lane_{decision_id}.json"


def _only_evidence_file(tmp_path):
    files = sorted((tmp_path / "evidence").glob("solana_lane_*.json"))
    assert len(files) == 1, f"expected one evidence file, got {files}"
    return files[0]


def _steps(tmp_path) -> list[dict]:
    raw = _only_evidence_file(tmp_path).read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _steps_for(tmp_path, runner):
    """Evidence steps of the most recent run in this workdir.

    The recovery-blocked run mints its own decision id, so its evidence file is
    the newest one rather than the one under test.
    """
    files = sorted(
        (tmp_path / "evidence").glob("solana_lane_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    raw = files[-1].read_text(encoding="utf-8")
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


def _mock_resolver_pool_rpc(
    m: aioresponses,
    *,
    rpc_url: str = _RPC_URL,
    genesis: str = SOLANA_MAINNET_GENESIS_HASH,
    health: str = "ok",
    repeat: bool = False,
) -> None:
    """The resolver pool's admission probe: getGenesisHash then getHealth.

    Registered before every other RPC mock because it is the first pair of
    calls ``place`` and ``resolve`` issue — an endpoint proves it is on
    mainnet-beta and caught up before anything reads a verdict off it.
    """
    m.post(rpc_url, payload=_rpc(genesis), repeat=repeat)
    m.post(rpc_url, payload=_rpc(health), repeat=repeat)


def _mock_pre_approval_rpc(
    m: aioresponses,
    *,
    sol_before: int = _SOL_BEFORE,
    usdc_before: int = 0,
    simulation_err=None,
    rpc_url: str = _RPC_URL,
    pool_probe: bool = True,
) -> None:
    """The RPC reads ``place`` issues up to the prompt.

    Order: resolver-pool genesis + health -> ATA rent -> SOL balance -> USDC
    balance -> simulate -> the display-time block height on the approval
    screen. ``pool_probe=False`` is for tests that register the pool's mocks
    themselves because a resolution runs in between.
    """
    if pool_probe:
        _mock_resolver_pool_rpc(m, rpc_url=rpc_url)
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
async def _seed_supervised_history(db, count: int) -> None:
    """Record ``count`` completed supervised executions, as the ledger holds them.

    Inserted directly rather than walked through the state machine: these rows
    stand in for trades that happened on earlier days, and the autonomy
    precondition reads exactly two columns — state and mode.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        for i in range(count):
            await db._conn.execute(
                "INSERT INTO solana_executions "
                "(decision_id, state, mode, created_at, updated_at) "
                "VALUES (?, 'reconciled', 'SUPERVISED_LIVE', ?, ?)",
                (f"historic-supervised-{i}", now, now),
            )
        await db._conn.commit()


async def _seed_solana_row(
    db: Database,
    *,
    decision_id: str,
    signature: str | None,
    status: str = "open",
    size_usd: str = "8.52",
    age_seconds: float = 0.0,
) -> int:
    anchor = await solana_lane.ensure_lane_anchor(db)
    async with db._txn_lock:
        cur = await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, status, client_order_id, entry_order_id, created_at)
               VALUES (?, 'solana-lane', 'SOL', 'solana', 'SOL/USDC',
                       'solana_lane', ?, ?, ?, ?, ?)""",
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
    return await solana_lane.fetch_row_by_decision_id(db, decision_id)


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
async def test_place_refuses_when_the_lane_is_disabled(tmp_path):
    runner, db, session = await _make_runner(tmp_path, SOLANA_MODE="DISABLED")
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert list(m.requests) == []  # nothing was asked of any venue
    assert "SOLANA_MODE" in _step(_steps(tmp_path), "aborted")["reason"]
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
    runner, db, session = await _make_runner(tmp_path, custody_ok=False)
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert list(m.requests) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "keypair_custody"
    assert "0o600" in abort["reason"]
    # The verdict came from stat; the key file itself was never opened.
    assert runner.custody_spy.calls == 1
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


def test_keyfile_policy_rejects_wide_permissions_and_foreign_owners():
    """The policy itself, exercised without depending on the host honouring chmod.

    Windows does not implement POSIX modes, so a policy only reachable through
    the filesystem would first be verified on trade day.
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
# Durable execution states
# ======================================================================
def test_every_state_has_a_transition_rule():
    """A state with no rule is a state the machine cannot reason about."""
    from scout.live import solana_lane as lane

    assert len(lane.ALL_STATES) == 13
    assert set(lane.LEGAL_TRANSITIONS) == set(lane.ALL_STATES)
    # Every destination is itself a real state.
    for origin, destinations in lane.LEGAL_TRANSITIONS.items():
        for destination in destinations:
            assert destination in lane.ALL_STATES, f"{origin} -> {destination}"


def test_a_run_must_begin_at_quote_created():
    from scout.live import solana_lane as lane

    lane.assert_legal_transition(None, lane.STATE_QUOTE_CREATED)
    for state in lane.ALL_STATES:
        if state == lane.STATE_QUOTE_CREATED:
            continue
        with pytest.raises(lane.IllegalStateTransition, match="must begin"):
            lane.assert_legal_transition(None, state)


def test_authorization_cannot_be_skipped():
    """*** The transition that would submit something nobody approved. ***"""
    from scout.live import solana_lane as lane

    with pytest.raises(lane.IllegalStateTransition):
        lane.assert_legal_transition(
            lane.STATE_AWAITING_AUTHORIZATION, lane.STATE_SUBMISSION_ATTEMPTED
        )
    with pytest.raises(lane.IllegalStateTransition):
        lane.assert_legal_transition(lane.STATE_SIMULATION_PASSED, lane.STATE_SIGNED)
    # The legal route runs through authorization, then signing.
    lane.assert_legal_transition(
        lane.STATE_AWAITING_AUTHORIZATION, lane.STATE_AUTHORIZED
    )
    lane.assert_legal_transition(lane.STATE_AUTHORIZED, lane.STATE_SIGNED)
    lane.assert_legal_transition(lane.STATE_SIGNED, lane.STATE_SUBMISSION_ATTEMPTED)


def test_no_state_can_walk_backwards_into_signing_or_submitting():
    """*** The anti-double-send property, stated over the whole machine. ***

    Not "the submit path does not retry" — that is asserted elsewhere on the
    POST count. This is stronger: from ANY state at or after submission, there
    is no path in the transition table that reaches signing or submitting
    again. A rebuild would need one.
    """
    from scout.live import solana_lane as lane

    post_submission = (
        lane.STATE_SUBMISSION_ATTEMPTED,
        lane.STATE_LANDED,
        lane.STATE_CONFIRMED,
        lane.STATE_FINALIZED,
        lane.STATE_SUBMISSION_UNKNOWN,
    )
    # Breadth-first over everything reachable from each post-submission state.
    for origin in post_submission:
        seen, frontier = set(), [origin]
        while frontier:
            current = frontier.pop()
            for nxt in lane.LEGAL_TRANSITIONS[current]:
                if nxt in seen:
                    continue
                seen.add(nxt)
                frontier.append(nxt)
        assert lane.STATE_SIGNED not in seen, f"{origin} can reach signing"
        assert (
            lane.STATE_SUBMISSION_ATTEMPTED not in seen
        ), f"{origin} can reach submission again"
        assert lane.STATE_QUOTE_CREATED not in seen, f"{origin} can restart"


def test_recovery_disposition_splits_on_whether_anything_was_sent():
    """The boundary the persistence exists for.

    Before submission a restart may discard the run without asking anyone;
    at or after it, the only permitted action is to ask the cluster.
    """
    from scout.live import solana_lane as lane

    for state in lane.PRE_SUBMISSION_STATES:
        assert lane.recovery_disposition(state) == "discard", state
    for state in lane.BLOCKING_STATES:
        assert lane.recovery_disposition(state) == "resolve", state
    for state in lane.TERMINAL_STATES:
        assert lane.recovery_disposition(state) == "done", state
    # Exhaustive: every state has a disposition, and the sets do not overlap.
    assert set(lane.PRE_SUBMISSION_STATES) | set(lane.BLOCKING_STATES) | set(
        lane.TERMINAL_STATES
    ) == set(lane.ALL_STATES)
    assert not set(lane.PRE_SUBMISSION_STATES) & set(lane.BLOCKING_STATES)


def test_terminal_states_go_nowhere():
    from scout.live import solana_lane as lane

    for state in lane.TERMINAL_STATES:
        assert lane.LEGAL_TRANSITIONS[state] == ()
        with pytest.raises(lane.IllegalStateTransition):
            lane.assert_legal_transition(state, lane.STATE_QUOTE_CREATED)


def test_an_unknown_state_is_rejected_rather_than_tolerated():
    from scout.live import solana_lane as lane

    with pytest.raises(lane.IllegalStateTransition, match="not an execution state"):
        lane.assert_legal_transition(lane.STATE_SIGNED, "nearly_submitted")
    with pytest.raises(lane.IllegalStateTransition, match="not an execution state"):
        lane.assert_legal_transition("halfway", lane.STATE_SIGNED)


# ======================================================================
# Durable execution store
# ======================================================================
async def _fresh_db(tmp_path, name="exec.db"):
    db = Database(tmp_path / name)
    await db.initialize()
    return db


async def test_the_store_persists_and_recovers_every_state(tmp_path):
    """*** Requirement 2: recovery at EVERY durable state, on real state. ***

    Each state is written, the connection is CLOSED, and a FRESH Database is
    opened against the same file — the closest a test gets to a process
    restart without forking. What comes back must be the state written and the
    disposition that state implies. Asserted on the recovered row, never on a
    mock.
    """
    from scout.live import solana_lane as lane

    walk = [
        lane.STATE_QUOTE_CREATED,
        lane.STATE_TRANSACTION_BUILT,
        lane.STATE_SIMULATION_PASSED,
        lane.STATE_AWAITING_AUTHORIZATION,
        lane.STATE_AUTHORIZED,
        lane.STATE_SIGNED,
        lane.STATE_SUBMISSION_ATTEMPTED,
        lane.STATE_LANDED,
        lane.STATE_CONFIRMED,
        lane.STATE_FINALIZED,
        lane.STATE_RECONCILED,
    ]
    decision_id = "walk-0001"
    for state in walk:
        db = await _fresh_db(tmp_path)
        await lane.record_execution_state(
            db, decision_id, state, mode="SUPERVISED_LIVE"
        )
        await db.close()

        # A brand-new connection: nothing in memory carried over.
        db = await _fresh_db(tmp_path)
        recovered = await lane.load_execution(db, decision_id)
        assert recovered is not None, state
        assert recovered["state"] == state
        expected = (
            "done"
            if state in lane.TERMINAL_STATES
            else "discard" if state in lane.PRE_SUBMISSION_STATES else "resolve"
        )
        assert lane.recovery_disposition(recovered["state"]) == expected, state
        await db.close()


async def test_recovery_scan_returns_only_interrupted_runs(tmp_path):
    from scout.live import solana_lane as lane

    db = await _fresh_db(tmp_path)
    await lane.record_execution_state(
        db, "live-1", lane.STATE_QUOTE_CREATED, mode="SUPERVISED_LIVE"
    )
    await lane.record_execution_state(
        db, "done-1", lane.STATE_QUOTE_CREATED, mode="SUPERVISED_LIVE"
    )
    await lane.record_execution_state(
        db, "done-1", lane.STATE_FAILED, mode="SUPERVISED_LIVE"
    )
    await db.close()

    db = await _fresh_db(tmp_path)
    recoverable = await lane.fetch_recoverable_executions(db)
    assert [r["decision_id"] for r in recoverable] == ["live-1"]
    await db.close()


async def test_the_store_refuses_an_illegal_transition(tmp_path):
    """The guard is at the WRITE, so the bad state never reaches the disk."""
    from scout.live import solana_lane as lane

    db = await _fresh_db(tmp_path)
    await lane.record_execution_state(
        db, "d1", lane.STATE_QUOTE_CREATED, mode="SUPERVISED_LIVE"
    )
    with pytest.raises(lane.IllegalStateTransition):
        await lane.record_execution_state(
            db, "d1", lane.STATE_SUBMISSION_ATTEMPTED, mode="SUPERVISED_LIVE"
        )
    assert (await lane.load_execution(db, "d1"))["state"] == lane.STATE_QUOTE_CREATED
    await db.close()


async def test_earlier_facts_survive_later_transitions(tmp_path):
    """The signature is written once and must outlive every step after it.

    A later transition knows less about the earlier ones than they did, so a
    naive UPDATE would blank the very field recovery depends on.
    """
    from scout.live import solana_lane as lane

    db = await _fresh_db(tmp_path)
    for state, fields in (
        (lane.STATE_QUOTE_CREATED, {"amount_lamports": 50_000_000}),
        (lane.STATE_TRANSACTION_BUILT, {"last_valid_block_height": 283_000_500}),
        (lane.STATE_SIMULATION_PASSED, {}),
        (lane.STATE_AWAITING_AUTHORIZATION, {"message_sha256": "abc123"}),
        (lane.STATE_AUTHORIZED, {}),
        (lane.STATE_SIGNED, {"expected_signature": "5sig"}),
        (lane.STATE_SUBMISSION_ATTEMPTED, {}),
    ):
        await lane.record_execution_state(
            db, "carry", state, mode="SUPERVISED_LIVE", **fields
        )
    await db.close()

    db = await _fresh_db(tmp_path)
    recovered = await lane.load_execution(db, "carry")
    assert recovered["state"] == lane.STATE_SUBMISSION_ATTEMPTED
    assert recovered["expected_signature"] == "5sig"
    assert recovered["last_valid_block_height"] == 283_000_500
    assert recovered["message_sha256"] == "abc123"
    assert recovered["amount_lamports"] == 50_000_000
    await db.close()


async def test_the_state_check_constraint_rejects_an_unknown_state(tmp_path):
    """Defence below the application guard: the database refuses it too."""
    import sqlite3

    db = await _fresh_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        await db._conn.execute(
            "INSERT INTO solana_executions "
            "(decision_id, state, mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("x", "almost_submitted", "SUPERVISED_LIVE", "t", "t"),
        )
    await db.close()


# ======================================================================
# Lifecycle <-> money coherence
# ======================================================================
def test_every_state_declares_its_coherent_statuses():
    from scout.live import solana_lane as lane

    assert set(lane.COHERENT_STATUSES) == set(lane.ALL_STATES)


def test_no_money_row_may_exist_before_authorization():
    """*** The defect a live_trades column would have introduced. ***

    quote_created through simulation_passed happen before anything financial
    does. A live_trades row there would read as an `open` position to every
    cross-venue consumer — the exposure view, daily gross, the Kraken gates.
    """
    from scout.live import solana_lane as lane

    for state in (
        lane.STATE_QUOTE_CREATED,
        lane.STATE_TRANSACTION_BUILT,
        lane.STATE_SIMULATION_PASSED,
    ):
        lane.assert_coherent(state, None)  # correct: no row yet
        with pytest.raises(lane.IncoherentLaneState, match="must not have"):
            lane.assert_coherent(state, "open")


def test_a_signed_execution_must_have_a_money_row():
    """The mirror: past authorization, a missing row is unrecoverable."""
    from scout.live import solana_lane as lane

    with pytest.raises(lane.IncoherentLaneState, match="requires a live_trades row"):
        lane.assert_coherent(lane.STATE_SIGNED, None)
    lane.assert_coherent(lane.STATE_SIGNED, "open")


def test_the_two_axes_cannot_disagree_about_blocking():
    """*** Coherence, stated as the question an operator actually asks. ***"""
    from scout.live import solana_lane as lane

    # An unknown submission blocks on the LIFECYCLE axis...
    assert lane.lane_is_blocked_by(lane.STATE_SUBMISSION_UNKNOWN, "needs_manual_review")
    lane.assert_coherent(lane.STATE_SUBMISSION_UNKNOWN, "needs_manual_review")

    # ...a completed swap blocks on the MONEY axis, because a position is held
    # even though the execution itself is finished. Different reasons, and the
    # blocker names which, so nobody chases the wrong table.
    assert "position is held" in lane.lane_is_blocked_by(lane.STATE_RECONCILED, "open")

    # A failed run blocks on neither.
    assert lane.lane_is_blocked_by(lane.STATE_FAILED, "rejected") is None
    lane.assert_coherent(lane.STATE_FAILED, "rejected")

    # Every blocking state names a reason rather than silently blocking.
    for state in lane.BLOCKING_STATES:
        assert lane.lane_is_blocked_by(state, None) is not None


# ======================================================================
# Operating modes + the authorization seam
# ======================================================================
@pytest.mark.parametrize(
    "mode,fragment",
    [
        ("DISABLED", "SOLANA_MODE is DISABLED"),
        ("EMERGENCY_STOPPED", "EMERGENCY_STOPPED"),
    ],
)
async def test_refusing_modes_refuse_before_anything_is_built(tmp_path, mode, fragment):
    """DISABLED and EMERGENCY_STOPPED stop at the envelope, key untouched."""
    runner, db, session = await _make_runner(tmp_path, SOLANA_MODE=mode)
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert list(m.requests) == []  # no venue was contacted at all
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "envelope_gate"
    assert fragment in abort["reason"]
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def test_emergency_stopped_and_the_kill_switch_refuse_in_the_same_places(
    tmp_path, monkeypatch
):
    """*** The configuration-side twin of the kill switch. ***

    EMERGENCY_STOPPED exists so an operator who sets it gets a refusal wherever
    the kill switch would have produced one. The two are checked at the same
    two boundaries — before anything is built, and again after authorization
    and before submission — and this pins BOTH boundaries for BOTH mechanisms
    so one can never drift ahead of the other.
    """
    from scout.live.kill_switch import KillSwitch

    # Boundary 1: before anything is built.
    runner, db, session = await _make_runner(tmp_path, SOLANA_MODE="EMERGENCY_STOPPED")
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert list(m.requests) == []
    assert _step(_steps(tmp_path), "aborted")["stage"] == "envelope_gate"
    await session.close()
    await db.close()

    for stale in (tmp_path / "evidence").glob("*.json"):
        stale.unlink()

    # Boundary 2: after authorization, before submission. The kill switch is
    # re-read there; the mode is fixed for the run by then, which is exactly
    # why the kill switch is the mechanism that can be engaged mid-run.
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _phrase(tx))
    runner, db, session = await _make_runner(tmp_path)
    # A REAL kill event, captured before the method is patched — calling the
    # patched method from inside the fake would recurse into the counter.
    ks = KillSwitch(db)
    await ks.trigger(
        triggered_by="manual", reason="ops drill", duration=timedelta(hours=1)
    )
    engaged_state = await ks.is_active()
    assert engaged_state is not None

    original = KillSwitch.is_active
    calls = {"n": 0}

    async def _engaged_after_approval(self):
        calls["n"] += 1
        # Clear on the pre-build check, engaged on the post-approval one.
        return None if calls["n"] == 1 else engaged_state

    monkeypatch.setattr(KillSwitch, "is_active", _engaged_after_approval)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    monkeypatch.setattr(KillSwitch, "is_active", original)

    steps = _steps(tmp_path)
    assert _step(steps, "kill_switch_recheck")["kill_active"] is True
    assert calls["n"] == 2, "the kill switch must be read at BOTH boundaries"
    # Authorized, then stopped: nothing signed, nothing sent, row retired.
    assert _step(steps, "signed_in_memory") is None
    assert runner.loader_spy.calls == 0
    assert (await _live_rows(db)) == [("rejected", None)]
    await session.close()
    await db.close()


async def test_bounded_autonomous_refuses_without_recorded_supervised_history(
    tmp_path,
):
    """*** The precondition configuration cannot fake. ***

    Mode set, flag on, envelope configured — and the lane still refuses,
    because the ledger holds no completed supervised execution. The transition
    is impossible by flipping one flag, which is the product mandate.
    """
    runner, db, session = await _make_runner(
        tmp_path,
        SOLANA_MODE="BOUNDED_AUTONOMOUS",
        SOLANA_BOUNDED_AUTONOMOUS_ENABLED=True,
        SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS=3,
    )
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert list(m.requests) == []  # refused before anything was contacted

    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "envelope_gate"
    assert "3 completed supervised execution(s)" in abort["reason"]
    assert "cannot be satisfied by configuration" in abort["reason"]
    assert runner.loader_spy.calls == 0

    # Two is still not three: the count is a threshold, not a boolean.
    await _seed_supervised_history(db, 2)
    for stale in (tmp_path / "evidence").glob("*.json"):
        stale.unlink()
    with aioresponses():
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
    assert "there are 2" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_the_autonomy_preconditions_are_recorded_in_the_evidence(tmp_path):
    """A promoted lane's evidence has to show WHY it was allowed to run."""
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(
        tmp_path,
        SOLANA_MODE="BOUNDED_AUTONOMOUS",
        SOLANA_BOUNDED_AUTONOMOUS_ENABLED=True,
        SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS=1,
    )
    await _seed_supervised_history(db, 1)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        await runner.place(sol=_SOL)

    envelope = _step(_steps(tmp_path), "envelope_gate")
    preconditions = envelope["autonomy_preconditions"]
    assert preconditions["enable_flag"] is True
    assert preconditions["supervised_reconciled"] == 1
    assert preconditions["supervised_required"] == 1
    # And the envelope it will trade inside, recorded with it.
    assert envelope["limits"]["daily_notional_usd"] == "25.0"
    await session.close()
    await db.close()


async def test_the_watchdog_command_reports_a_stuck_execution(tmp_path, capsys):
    """The lane's own CLI surface for the stuck-state watchdog, for cron.

    Exit code distinguishes healthy from stuck so a wrapper does not parse
    stdout.
    """
    from datetime import timedelta as _timedelta

    from scout.live.solana import execution_watchdog

    execution_watchdog._reset_for_tests()
    runner, db, session = await _make_runner(tmp_path)
    assert await runner.watchdog(session) == EXIT_OK
    assert "no stuck executions" in capsys.readouterr().out

    stale = (datetime.now(timezone.utc) - _timedelta(hours=2)).isoformat()
    async with db._txn_lock:
        await db._conn.execute(
            "INSERT INTO solana_executions "
            "(decision_id, state, mode, expected_signature, created_at, updated_at) "
            "VALUES ('stuck-1', 'submission_attempted', 'SUPERVISED_LIVE', "
            "'sig', ?, ?)",
            (stale, stale),
        )
        await db._conn.commit()

    assert await runner.watchdog(session) == EXIT_BLOCKED
    out = capsys.readouterr().out
    assert "STUCK EXECUTION" in out
    assert "A TRANSACTION MAY EXIST" in out
    assert "NEVER rebuild" in out
    execution_watchdog._reset_for_tests()
    await session.close()
    await db.close()


async def test_an_unrecognised_mode_refuses_rather_than_defaulting(tmp_path):
    """A typo in .env must not fall back to some 'safe' behaviour.

    Falling back would mean the mode an operator THINKS is in force and the
    one actually in force can differ, which is the property the mode exists to
    remove.
    """
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        assert await runner.place(sol=_SOL, mode="SUPERVISED") == EXIT_REFUSED
        assert list(m.requests) == []
    assert "not a recognised mode" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_bounded_autonomous_needs_a_second_deliberate_setting(tmp_path):
    """*** The mode alone must not start autonomous execution. ***"""
    runner, db, session = await _make_runner(tmp_path, SOLANA_MODE="BOUNDED_AUTONOMOUS")
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert list(m.requests) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert "SOLANA_BOUNDED_AUTONOMOUS_ENABLED is False" in abort["reason"]
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def test_bounded_autonomous_swaps_the_policy_and_nothing_else(tmp_path, capsys):
    """*** The seam. ***

    With both settings on, the lane runs the SAME path — quote, build,
    inspect, balance, simulate — and differs only in who is asked. No prompt
    is printed, and the phase-5 policy refuses rather than approving by
    default, so an unfinished policy cannot trade.
    """
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(
        tmp_path,
        SOLANA_MODE="BOUNDED_AUTONOMOUS",
        SOLANA_BOUNDED_AUTONOMOUS_ENABLED=True,
        SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS=3,
    )
    # The mode and the flag are not enough on their own — the lane also
    # requires recorded supervised history. See
    # test_bounded_autonomous_refuses_without_recorded_supervised_history.
    await _seed_supervised_history(db, 3)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    # It got all the way to the authorization boundary on the normal path.
    names = _step_names(steps)
    assert "tx_inspection" in names and "simulation" in names
    authorization = _step(steps, "authorization")
    assert authorization["method"] == "bounded_autonomous_policy"
    assert authorization["outcome"] == "authorization_refused"
    assert authorization["detail"] == "autonomous_policy_not_yet_implemented"
    assert authorization["prompted"] is False
    # No human was asked, and the key was never read.
    assert "MANUAL APPROVAL REQUIRED" not in capsys.readouterr().out
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def test_supervised_live_uses_the_typed_policy(tmp_path, monkeypatch):
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _phrase(tx))
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_happy_path(m, tx)
        assert await runner.place(sol=_SOL) == EXIT_OK
    authorization = _step(_steps(tmp_path), "authorization")
    assert authorization["method"] == "typed_operator_authorization"
    assert authorization["prompted"] is True
    assert authorization["mode"] == "SUPERVISED_LIVE"
    await session.close()
    await db.close()


def test_the_two_executing_modes_share_one_authorization_call_site():
    """*** Structural: autonomy is a policy swap, not a second code path. ***

    The completion standard says moving to bounded autonomy must be
    configuration and authorization only. That is only true if `place()` asks
    ONE question in ONE place — a second call site is where the two modes would
    start to diverge.
    """
    import ast
    import inspect

    from scout.live import solana_lane as module

    tree = ast.parse(inspect.getsource(module))
    place = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "place"
    )
    authorize_calls = [
        n
        for n in ast.walk(place)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "authorize"
    ]
    assert len(authorize_calls) == 1, (
        "place() must ask exactly one authorization question; found "
        f"{len(authorize_calls)}"
    )
    # `mode in EXECUTING_MODES` is legitimate and expected — it is the
    # execute-or-rehearse decision. What must NOT appear is place() knowing
    # WHICH executing mode it is in: the moment it can tell supervised from
    # autonomous, the two have somewhere to diverge.
    named = [
        n.id
        for n in ast.walk(place)
        if isinstance(n, ast.Name)
        and n.id in ("MODE_SUPERVISED_LIVE", "MODE_BOUNDED_AUTONOMOUS")
    ]
    assert not named, (
        "place() references a specific executing mode "
        f"({sorted(set(named))}); that distinction belongs to the policy "
        "factory, so supervised and autonomous cannot diverge here"
    )
    literals = [
        n.value
        for n in ast.walk(place)
        if isinstance(n, ast.Constant)
        and n.value in ("SUPERVISED_LIVE", "BOUNDED_AUTONOMOUS")
    ]
    assert not literals, f"place() hardcodes a mode name: {sorted(set(literals))}"


def test_every_mode_maps_to_a_policy():
    from scout.live import solana_lane

    for mode in solana_lane.ALL_MODES:
        policy = solana_lane.authorization_policy_for(mode)
        assert policy.method != "undefined"
    # Only the autonomous mode gets the non-interactive policy.
    assert (
        solana_lane.authorization_policy_for("BOUNDED_AUTONOMOUS").method
        == "bounded_autonomous_policy"
    )
    for mode in ("SIMULATION_ONLY", "SUPERVISED_LIVE"):
        assert (
            solana_lane.authorization_policy_for(mode).method
            == "typed_operator_authorization"
        )


def test_simulate_only_can_only_narrow_the_mode():
    """The CLI flag restricts; it can never escalate a configured mode."""
    args = build_parser().parse_args(
        ["place", "--sol", "0.05", "--simulate-only", "--yes-i-am-rehearsing"]
    )
    assert args.simulate_only is True
    # There is deliberately no flag that can SET an executing mode.
    assert not any(
        "supervised" in (a or "").lower() or "autonomous" in (a or "").lower()
        for a in vars(args)
    )


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
        _mock_resolver_pool_rpc(m)
        m.get(_QUOTE_RE, payload=_quote_payload(out_amount=out_raw))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert _posts_to(m, _SWAP_URL) == []  # refused before Jupiter built anything
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "quote_envelope"
    assert "per_trade_notional_within_band" in abort["reason"]
    # The quote step is written BEFORE the limits are enforced, so the refused
    # run leaves the same structured record a passing one would.
    limits = _step(_steps(tmp_path), "quote")["limits"]
    assert limits["passed"] is False
    assert limits["failed_checks"] == ["per_trade_notional_within_band"]
    await session.close()
    await db.close()


async def test_place_refuses_when_price_impact_exceeds_the_ceiling(tmp_path):
    """Jupiter reports priceImpactPct as a FRACTION — 0.02 is 2%, not 0.02%."""
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m)
        m.get(_QUOTE_RE, payload=_quote_payload(swapMode="ExactOut"))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert "swap_mode_is_exact_in" in _step(_steps(tmp_path), "aborted")["reason"]
    await session.close()
    await db.close()


async def test_place_refuses_a_quote_with_looser_slippage_than_approved(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_resolver_pool_rpc(m)
        m.get(_QUOTE_RE, payload=_quote_payload(slippageBps=500))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert "slippage_within_ceiling" in _step(_steps(tmp_path), "aborted")["reason"]
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
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m)
        _mock_quote_and_build(m, tx)
        m.post(_RPC_URL, payload=_rpc(_ATA_RENT))
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": barely}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "balance"
    assert "sol_covers_swap_fees_and_headroom" in abort["reason"]
    # The rent is inside the required figure, which is what the gate refused on.
    assert "fees and rent" in abort["reason"]
    balance = _step(_steps(tmp_path), "balance")
    assert balance["limits"]["passed"] is False  # recorded, and recorded FAILING
    assert balance["ata_create_count"] == 1
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
            _mock_resolver_pool_rpc(m)
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
    """An RPC hiccup on rent must not block the lane, but must be visible."""
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(tmp_path)

    async def _unreachable(_data_length):
        raise TimeoutError("rent lookup timed out")

    monkeypatch.setattr(
        runner._rpc, "get_minimum_balance_for_rent_exemption", _unreachable
    )
    with aioresponses() as m:
        _mock_resolver_pool_rpc(m)
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

    from scout.live import solana_lane as module

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
    real_sign = solana_lane.sign_transaction

    def _sign_something_else(tx_b64, keypair, **kwargs):
        # Signs a DIFFERENT transaction with the same key, so the signature
        # is valid and the digest simply does not match what was inspected.
        return real_sign(
            build_swap_tx(tip_lamports=99_999).tx_b64,
            keypair,
            expected_signer=PAYER_PUBKEY,
        )

    monkeypatch.setattr(solana_lane, "sign_transaction", _sign_something_else)
    # The operator authorizes the REAL transaction's hash; the signer then
    # produces a signature over different bytes.
    _authorize(monkeypatch, _phrase(tx))
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        _mock_post_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "signed_in_memory"
    assert "does not match the AUTHORIZED digest" in abort["reason"]
    # The row is terminal already — this path retires its own row rather
    # than leaving it for a later run's unsigned-row sweep.
    assert await _live_rows(db) == [("rejected", None)]
    assert _step(_steps(tmp_path), "intent_retired")["ledger_status"] == "rejected"
    await session.close()
    await db.close()


# ======================================================================
# S1-review requirements 4 + 5: the inspector gets live inputs, not defaults
# ======================================================================
async def test_the_live_jito_tip_list_is_used_and_its_provenance_recorded(
    tmp_path, monkeypatch
):
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _phrase(tx))
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
        _mock_resolver_pool_rpc(m)
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
    _authorize(monkeypatch, _phrase(built.tx_b64))

    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_resolver_pool_rpc(m)
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
    _authorize(monkeypatch, _phrase(tx))
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_RPC_URL=_ROUND_ROBIN_RPC_URL
    )
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m, rpc_url=_ROUND_ROBIN_RPC_URL)
        assert await runner.place(sol=_SOL, mode="SIMULATION_ONLY") == EXIT_OK
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
    _authorize(monkeypatch, _phrase(tx))
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL, mode="SIMULATION_ONLY") == EXIT_OK
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
    _authorize(monkeypatch, _phrase(tx))
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
            assert await runner.place(sol=_SOL, mode="SIMULATION_ONLY") == EXIT_OK
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

    from scout.live import solana_lane as module

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

    # An `if not executing:` whose body returns — the guard itself. `executing`
    # is `mode in EXECUTING_MODES`, so this is the mode check that decides
    # whether the funded key is ever read.
    guard_returns = [
        stmt.lineno
        for node in ast.walk(place)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "executing"
        for stmt in node.body
        if isinstance(stmt, ast.Return)
    ]
    assert guard_returns, "no `if not executing: ... return` guard in place()"
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
    # A LABEL, not the URL: provider endpoints carry the API key in the path.
    assert envelope["resolver_rpc_url"] == redact_endpoint(_RPC_URL)
    assert _RPC_URL not in json.dumps(envelope)
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
        _mock_resolver_pool_rpc(m, rpc_url=_ROUND_ROBIN_RPC_URL)
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
    from scout.live.solana_lane import resolver_endpoint

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
# Resolver pool: chain identity, secrecy, and the all-endpoints pin rule
# ======================================================================
async def test_place_refuses_when_the_resolver_is_not_on_mainnet(tmp_path):
    """*** The verdict-poisoning case, at the lane boundary. ***

    A devnet node reports every mainnet signature as absent, and absence is
    half of the one verdict that clears the lane. The URL says nothing about
    which chain is behind it; getGenesisHash does. Nothing is quoted, built or
    sent.
    """
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(_DEVNET_GENESIS))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert _posts_to(m, _QUOTE_RE) == []

    steps = _steps(tmp_path)
    abort = _step(steps, "aborted")
    assert abort["stage"] == "resolver_pool"
    assert "mainnet-beta" in abort["reason"]
    pool = _step(steps, "resolver_pool")
    assert pool["usable_count"] == 0
    assert pool["endpoints"][0]["genesis_ok"] is False
    await session.close()
    await db.close()


async def test_a_rehearsal_proceeds_past_a_failed_pool_and_says_so(
    tmp_path, monkeypatch, capsys
):
    """A rehearsal provably never submits, so it can never need the resolver.

    Refusing one for the quality of an endpoint it will not read would block
    the run that exists to exercise everything else — but it must not read as
    a launch-ready lane either.
    """
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, None)  # stop at the approval boundary
    runner, db, session = await _make_runner(tmp_path, SOLANA_MODE="SIMULATION_ONLY")
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc(_DEVNET_GENESIS))
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m, pool_probe=False)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    assert _step(steps, "resolver_pool")["usable_count"] == 0
    assert _step(steps, "quote") is not None, "the rehearsal was blocked by the pool"
    assert "NOTICE: no resolver endpoint passed" in capsys.readouterr().out
    await session.close()
    await db.close()


async def test_one_load_balanced_endpoint_in_the_pool_refuses_the_whole_lane(tmp_path):
    """Pinning is an ALL-endpoint property.

    Selection is by signature, so every endpoint in the pool eventually serves
    a resolution — one round-robin member is enough to manufacture a false
    'definitively_not_submitted' on some future signature.
    """
    runner, db, session = await _make_runner(
        tmp_path,
        SOLANA_RESOLVER_RPC_URLS=f"{_RPC_URL},{_ROUND_ROBIN_RPC_URL}",
    )
    with aioresponses() as m:
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert list(m.requests) == []  # refused before any network call

    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "envelope_gate"
    assert "load-balanced" in abort["reason"]
    assert "api.mainnet-beta.solana.com" in abort["reason"]
    await session.close()
    await db.close()


async def test_a_resolver_url_carrying_an_api_key_never_reaches_the_evidence(tmp_path):
    """Provider endpoints put the key IN the URL. Evidence is a durable file."""
    keyed = "https://solana-mainnet.g.alchemy.com/v2/SECRET-LANE-KEY-DO-NOT-LEAK"
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_RPC_URL=keyed, SOLANA_RESOLVER_RPC_URLS=keyed
    )
    with aioresponses() as m:
        _mock_resolver_pool_rpc(m, rpc_url=keyed)
        # Refuse at the quote so the run stays short; the pool step is already
        # written by then.
        m.get(_QUOTE_RE, payload=_quote_payload(out_amount=4_990_000))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED

    raw = _only_evidence_file(tmp_path).read_text(encoding="utf-8")
    assert "SECRET-LANE-KEY-DO-NOT-LEAK" not in raw
    assert "solana-mainnet.g.alchemy.com#" in raw  # labelled, not silent
    pool = _step(_steps(tmp_path), "resolver_pool")
    assert pool["usable_count"] == 1
    await session.close()
    await db.close()


async def test_a_runner_built_without_a_pool_labels_the_client_it_actually_reads(
    tmp_path,
):
    """The fallback wiring must not label an endpoint it does not read from.

    `LaneRunner` takes its clients by injection, so a caller may omit the pool.
    The one-endpoint pool it then builds has to describe the client it was
    handed — a label taken from config instead would name a node the runner
    never contacts, which is worse than no label at all.
    """
    import aiohttp as _aiohttp

    settings = _settings(
        tmp_path,
        SOLANA_RPC_URL=_RPC_URL,
        SOLANA_RESOLVER_RPC_URL="https://never-contacted.example/rpc",
    )
    db = Database(tmp_path / "lane.db")
    await db.initialize()
    async with _aiohttp.ClientSession() as session:
        runner = LaneRunner(
            settings=settings,
            db=db,
            jupiter=JupiterClient(settings, session),
            rpc=SolanaRpcClient(settings, session),
            jito=JitoClient(settings, session),
            kill_switch=KillSwitch(db),
        )
        assert runner._pool.size == 1
        assert runner._pool.labels == (redact_endpoint(_RPC_URL),)
    await db.close()


# ======================================================================
# Limits engine, enforced end to end
# ======================================================================
async def test_place_refuses_when_the_daily_cap_is_already_spent(tmp_path):
    """*** The limit that bounds a runaway rather than one bad trade. ***

    Per-trade caps bound one mistake; the daily cap is what bounds a loop. It
    counts AUTHORIZED notional, so an earlier trade that has already closed
    still consumes the day's budget.
    """
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_MAX_DAILY_NOTIONAL_USD=25.0
    )
    # 20 USD authorized earlier today and since closed. It does not block the
    # lane, but it has spent the budget.
    await _seed_solana_row(
        db,
        decision_id="8f0e0f5a-4d21-4d0e-bb2c-9f31d0f4a001",
        signature=None,
        status="closed_tp",
        size_usd="20.00",
    )
    with aioresponses() as m:
        _mock_resolver_pool_rpc(m)
        m.get(_QUOTE_RE, payload=_quote_payload())
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        assert _posts_to(m, _SWAP_URL) == []  # never built anything

    steps = _steps(tmp_path)
    abort = _step(steps, "aborted")
    assert abort["stage"] == "quote_envelope"
    assert "daily_notional_within_cap" in abort["reason"]
    quote = _step(steps, "quote")
    assert quote["exposure"]["notional_usd_today"] == "20.00"
    assert quote["limits"]["failed_checks"] == ["daily_notional_within_cap"]
    await session.close()
    await db.close()


async def test_the_quote_evidence_records_every_limit_that_was_evaluated(tmp_path):
    """A limit that silently does not apply looks the same in a log as one
    that passed. The evidence records what was PROVED, not only what broke."""
    tx = build_swap_tx().tx_b64
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        await runner.place(sol=_SOL)  # stops at the prompt on closed stdin

    limits = _step(_steps(tmp_path), "quote")["limits"]
    assert limits["passed"] is True
    names = {c["name"] for c in limits["checks"]}
    assert {
        "per_trade_notional_within_band",
        "daily_notional_within_cap",
        "open_positions_within_limit",
        "concurrent_executions_within_limit",
        "input_mint_allowed",
        "output_mint_allowed",
        "route_labels_allowed",
        "slippage_within_ceiling",
        "price_impact_within_ceiling",
    } <= names
    await session.close()
    await db.close()


async def test_a_limit_the_inspector_does_not_enforce_still_refuses(tmp_path):
    """The inspector answers "are these the bytes we asked for"; the limits
    answer "are we allowed to spend this". Both are enforced, separately.

    The account-create cap is the clean demonstration: the inspector counts ATA
    rent into the fee total but has no opinion on how many accounts a build may
    open, so a build that passes inspection completely is still refused here.
    """
    runner, db, session = await _make_runner(tmp_path, SOLANA_PILOT_MAX_ATA_CREATES=0)
    with aioresponses() as m:
        _mock_resolver_pool_rpc(m)
        _mock_quote_and_build(m, build_swap_tx().tx_b64)  # emits one ATA create
        m.post(_RPC_URL, payload=_rpc(_ATA_RENT))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    abort = _step(steps, "aborted")
    assert abort["stage"] == "tx_limits"
    assert "ata_creates_within_limit" in abort["reason"]
    # The inspection itself passed — this is a spend limit, not a bad build.
    inspection = _step(steps, "tx_inspection")
    assert inspection["passed"] is True
    assert inspection["limits"]["failed_checks"] == ["ata_creates_within_limit"]
    await session.close()
    await db.close()


async def test_a_stale_quote_invalidates_the_authorization(tmp_path, monkeypatch):
    """*** What the blockhash check alone does not cover. ***

    The approval prompt is unbounded operator time. An expired blockhash cannot
    land at all; a stale quote lands at a price the operator never saw. The
    on-chain minimum-output bound protects the trade — nothing but this
    protects the intent.
    """
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _phrase(tx))
    runner, db, session = await _make_runner(tmp_path, SOLANA_MAX_QUOTE_AGE_SEC=0.001)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        _mock_post_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    freshness = _step(steps, "blockhash_recheck")
    assert freshness["valid"] is False
    assert freshness["limits"]["failed_checks"] == ["quote_age_within_limit"]
    # The blockhash itself was fine — it is the quote that went stale.
    assert freshness["blocks_remaining"] > 0
    # Nothing was signed, and the row asserting an intent is retired.
    assert _step(steps, "signed_in_memory") is None
    assert runner.loader_spy.calls == 0
    assert (await _live_rows(db)) == [("rejected", None)]
    await session.close()
    await db.close()


async def test_the_approval_screen_shows_the_envelope_it_was_judged_against(
    tmp_path, monkeypatch, capsys
):
    """An operator authorizing a number should not have to reconstruct the
    bounds it cleared from a config file."""
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, None)
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        await runner.place(sol=_SOL)

    out = capsys.readouterr().out
    assert "envelope" in out
    assert "per-trade 5.0-10.0 USD" in out
    assert "daily cap 25.0 USD" in out
    await session.close()
    await db.close()


async def test_the_pool_is_built_from_the_ordered_url_list(tmp_path):
    """Adding an endpoint is configuration — this is the wiring that proves it."""
    import aiohttp as _aiohttp

    from scout.live.solana.resolver_pool import ResolverPool

    settings = _settings(
        tmp_path, SOLANA_RESOLVER_RPC_URLS=f"{_RPC_URL},https://second-node.example/rpc"
    )
    async with _aiohttp.ClientSession() as session:
        pool = ResolverPool.from_settings(settings, session)
        assert pool.size == 2
        assert len(set(pool.labels)) == 2
        assert [e.client._url for e in pool.endpoints] == [
            _RPC_URL,
            "https://second-node.example/rpc",
        ]


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
    # *** The funded key was never read. *** This is what the reordering
    # exists for: a refused authorization must not leave a broadcastable
    # artifact behind, and the only way to be sure is that nothing signed.
    assert runner.loader_spy.calls == 0
    assert _step(steps, "signed_in_memory") is None
    assert _step(steps, "funded_signer_loaded") is None
    # The intent row recorded what was being authorized and is now retired,
    # still carrying no signature.
    assert _step(steps, "intent_persisted")["entry_order_id"] is None
    assert _step(steps, "intent_retired")["ledger_status"] == "rejected"
    assert await _live_rows(db) == [("rejected", None)]
    await session.close()
    await db.close()


async def test_the_authorization_phrase_is_the_message_hash_prefix(
    tmp_path, monkeypatch, capsys
):
    """*** Authorization binds to the message, and precedes any signature. ***

    The screen cannot show an expected signature, because none exists when
    the operator is asked — the funded key has not been read. It shows the
    sha256 of the exact bytes the key will sign, and the operator types the
    first 8 characters of that.
    """
    tx = build_swap_tx().tx_b64
    message_hash = _message_sha256(tx)
    _authorize(monkeypatch, _phrase(tx))
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_happy_path(m, tx)
        assert await runner.place(sol=_SOL) == EXIT_OK
        assert len(_submissions(m)) == 1

    out = capsys.readouterr().out
    assert f"MESSAGE SHA256      : {message_hash}" in out
    assert "first 8 characters of the MESSAGE SHA256" in out
    # The screen must not promise a signature it cannot have.
    assert "EXPECTED SIGNATURE" not in out
    assert "NOT SIGNED - the funded key is read only after you authorize" in out

    steps = _steps(tmp_path)
    assert _step(steps, "authorization")["bound_to_message_sha256"] == message_hash
    assert _step(steps, "authorization")["funded_signer_loaded"] is False
    # ...and the hash the operator approved is the hash that got signed.
    assert _step(steps, "signed_in_memory")["message_sha256"] == message_hash
    assert _step(steps, "signed_in_memory")["matches_authorized_hash"] is True
    await session.close()
    await db.close()


@pytest.mark.parametrize(
    "field,variant",
    [
        ("amount", dict(route_amount_lamports=60_000_000)),
        ("slippage", dict(route_slippage_bps=300)),
        ("blockhash", dict(blockhash="9zjmVQzTaMSKtP2rWXvcH6EABKPBGKGKZfNmMbT7xkFq")),
        ("tip", dict(tip_lamports=123_456)),
        ("priority fee", dict(compute_unit_price=9_999)),
    ],
)
def test_changing_any_message_field_invalidates_the_authorization(field, variant):
    """Every one of these lives IN the message, so each moves the hash.

    That is what makes the hash safe to bind an authorization to: an
    operator who approved one set of numbers cannot have that approval spent
    on another, because the phrase they typed no longer matches.
    """
    baseline = _message_sha256(build_swap_tx().tx_b64)
    changed = _message_sha256(build_swap_tx(**variant).tx_b64)
    assert changed != baseline, f"{field} is not represented in the message"
    assert changed[:8] != baseline[:8], f"{field} did not change the phrase"


async def test_a_phrase_for_a_different_transaction_does_not_authorize(
    tmp_path, monkeypatch
):
    """End-to-end companion to the hash-level check above."""
    tx = build_swap_tx().tx_b64
    other = build_swap_tx(route_amount_lamports=60_000_000).tx_b64
    _authorize(monkeypatch, _phrase(other))
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def test_the_funded_key_is_not_read_on_any_pre_authorization_refusal(
    tmp_path,
):
    """*** Required property 2, swept across the gates. ***

    Every refusal reachable before the prompt must leave the key file
    unopened — not merely unused. Asserted on the loader's call count,
    because an absence is only checkable if the thing absent is observable.
    """
    cases = {
        "disabled": dict(SOLANA_MODE="DISABLED"),
        "no_signer_declared": dict(SOLANA_PILOT_SIGNER_PUBKEY=""),
        "devnet_host": dict(SOLANA_RPC_URL="https://api.devnet.solana.com"),
    }
    for label, overrides in cases.items():
        workdir = tmp_path / label
        workdir.mkdir()
        runner, db, session = await _make_runner(workdir, **overrides)
        with aioresponses() as m:
            assert await runner.place(sol=_SOL) == EXIT_REFUSED, label
            assert _submissions(m) == [], label
        assert runner.loader_spy.calls == 0, label
        await session.close()
        await db.close()

    # ...and for a refusal deep in the flow, after the venue was called.
    workdir = tmp_path / "band"
    workdir.mkdir()
    runner, db, session = await _make_runner(workdir)
    with aioresponses() as m:
        m.get(_QUOTE_RE, payload=_quote_payload(out_amount=4_990_000))
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
    assert runner.loader_spy.calls == 0
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
    _authorize(monkeypatch, _phrase(tx))
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    assert _step(steps, "kill_switch_recheck")["kill_active"] is True
    # The kill re-check runs before signing, so the row retires still holding
    # no signature — and the funded key was never read.
    assert await _live_rows(db) == [("rejected", None)]
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def test_stale_blockhash_invalidates_the_authorization(
    tmp_path, monkeypatch, capsys
):
    """The operator read the screen too slowly; the numbers went stale."""
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _phrase(tx))
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
    assert await _live_rows(db) == [("rejected", None)]
    # Stale numbers are caught before the key is read, so a rerun is a clean
    # loop rather than a second signed artifact.
    assert runner.loader_spy.calls == 0
    out = capsys.readouterr().out
    assert "AUTHORIZATION INVALIDATED" in out
    assert "a NEW quote, a NEW message hash, and a NEW" in out
    await session.close()
    await db.close()


async def test_unreadable_block_height_after_approval_fails_closed(
    tmp_path, monkeypatch
):
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _phrase(tx))
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
    monkeypatch.setattr(solana_lane, "load_settings", lambda: _settings(tmp_path))
    assert (
        await solana_lane.main(["place", "--sol", "0.05", "--simulate-only"])
        == EXIT_REFUSED
    )
    assert "requires --yes-i-am-rehearsing" in capsys.readouterr().out
    assert (
        await solana_lane.main(["place", "--sol", "0.05", "--yes-i-am-rehearsing"])
        == EXIT_REFUSED
    )
    assert "This would be a REAL swap" in capsys.readouterr().out


async def test_simulate_only_never_reads_the_funded_key(tmp_path, monkeypatch, capsys):
    """*** Required property 1. ***

    A rehearsal runs the whole flow — inspection, simulation, the approval
    prompt — and stops before the one step that creates something
    irreversible. A validly signed transaction is a broadcastable artifact
    whoever holds it, so a rehearsal has no business producing one.
    """
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _phrase(tx))
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        assert await runner.place(sol=_SOL, mode="SIMULATION_ONLY") == EXIT_OK
        assert _submissions(m) == []

    steps = _steps(tmp_path)
    names = _step_names(steps)
    # Everything except signing really happened, including the prompt.
    assert "tx_inspection" in names and "simulation" in names
    assert _step(steps, "authorization")["outcome"] == "authorized"

    # *** The funded key was never read, and nothing was signed. ***
    assert runner.loader_spy.calls == 0
    assert "signed_in_memory" not in names
    assert "funded_signer_loaded" not in names
    assert _step(steps, "rehearsal_complete")["funded_signer_loaded"] is False
    # Custody was still verified — from stat, without opening the file.
    assert runner.custody_spy.calls == 1
    assert _step(steps, "keypair_custody")["key_material_read"] is False

    # No signature appears anywhere in the evidence, because none exists.
    raw = _only_evidence_file(tmp_path).read_text(encoding="utf-8")
    assert _expected_signature(tx) not in raw
    assert _message_sha256(tx) in raw

    # And no ledger row: a row would be a phantom blocking the next run.
    assert "intent_persisted" not in names
    assert await _count(db, "live_trades") == 0
    out = capsys.readouterr().out
    assert "nothing was signed, nothing was sent" in out
    assert "funded key     : NOT read" in out
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
    _authorize(monkeypatch, _phrase(tx))
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
    assert "LANE SWAP FINALIZED" in capsys.readouterr().out
    await session.close()
    await db.close()


async def test_the_signature_is_persisted_before_the_jito_call(tmp_path, monkeypatch):
    """*** Required property 6. ***

    The idempotency key has to be durable before the POST, or a crash right
    after submission leaves a transaction nobody can resolve. The whole
    ordering is asserted here in one place, because each step's meaning
    depends on the ones around it.
    """
    tx = build_swap_tx().tx_b64
    signature = _expected_signature(tx)
    _authorize(monkeypatch, _phrase(tx))
    runner, db, session = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_happy_path(m, tx)
        assert await runner.place(sol=_SOL) == EXIT_OK

    steps = _steps(tmp_path)
    names = _step_names(steps)

    # What is being authorized is durable BEFORE the prompt...
    intent = _step(steps, "intent_persisted")
    assert intent["message_sha256"] == _message_sha256(tx)
    assert intent["last_valid_block_height"] == _LAST_VALID
    assert intent["entry_order_id"] is None
    assert names.index("intent_persisted") < names.index("authorization")

    # ...the key is read only after it...
    assert names.index("authorization") < names.index("funded_signer_loaded")
    assert names.index("funded_signer_loaded") < names.index("signed_in_memory")

    # ...and the signature is committed, read back, and only then sent.
    persisted = _step(steps, "signature_persisted")
    assert persisted["expected_signature"] == signature
    assert persisted["confirmed_by_read_back"] is True
    assert names.index("signed_in_memory") < names.index("signature_persisted")
    assert names.index("signature_persisted") < names.index("submitted")
    await session.close()
    await db.close()


async def test_an_interruption_after_signing_leaves_a_recoverable_row(
    tmp_path, monkeypatch
):
    """*** Required property 7, the crash-after-signing half. ***

    The dangerous window is between the signature being committed and the POST
    completing: a transaction may or may not exist. The row must survive
    carrying its signature, and the NEXT run must resolve it — never resubmit
    it, because a rebuild or a resend is how one authorization becomes two
    swaps.
    """
    tx = build_swap_tx().tx_b64
    signature = _expected_signature(tx)
    _authorize(monkeypatch, _phrase(tx))
    runner, db, session = await _make_runner(tmp_path)

    def _interrupt(*_args, **_kwargs):
        # Not an exception the runner handles — this is the process going away
        # between the durable write and the block engine answering.
        raise KeyboardInterrupt("operator interrupted mid-submission")

    monkeypatch.setattr(JitoClient, "submit_transaction", _interrupt)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        _mock_post_approval_rpc(m)
        with pytest.raises(KeyboardInterrupt):
            await runner.place(sol=_SOL)

    # The signature was durable BEFORE the interruption, so the row is
    # recoverable rather than a mystery.
    assert await _live_rows(db) == [("open", signature)]
    steps = _steps(tmp_path)
    assert _step(steps, "signature_persisted")["expected_signature"] == signature
    assert _step(steps, "submitted") is None
    decision_id = _step(steps, "run_started")["decision_id"]
    await session.close()

    # A FRESH runner against the same database resolves and never submits.
    monkeypatch.undo()
    runner2, db2, session2 = await _make_runner(tmp_path)

    def _detonate(*_args, **_kwargs):
        raise AssertionError("the recovery path submitted something")

    monkeypatch.setattr(JitoClient, "submit_transaction", _detonate)
    with aioresponses() as m:
        _mock_resolver_pool_rpc(m)
        # Absent and expired: the transaction never landed, so the lane clears.
        for _ in range(2):
            m.post(_RPC_URL, payload=_sig_status(known=False))
            m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        assert await runner2.resolve(decision_id=decision_id) == EXIT_OK
        assert _submissions(m) == []

    assert (await _row(db2, decision_id))["status"] == "rejected"
    await session2.close()
    await db2.close()
    await db.close()


def test_the_submit_call_cannot_precede_the_signature_write():
    """Structural companion: the ordering above, asserted over the AST.

    A runtime test shows the order on the path it exercised. This shows it
    for the function — and the NULL-signature ledger state means 'provably
    never submitted' only because this ordering holds.
    """
    import ast
    import inspect

    from scout.live import solana_lane as module

    tree = ast.parse(inspect.getsource(module))
    place = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "place"
    )
    persist = [
        n.lineno
        for n in ast.walk(place)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", None))
        == "persist_expected_signature"
    ]
    submit = [
        n.lineno
        for n in ast.walk(place)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) == "_submit_and_resolve"
    ]
    assert persist, "expected the signature-persist call site"
    assert submit, "expected the submission call site"
    assert max(persist) < min(submit)


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
    _authorize(monkeypatch, _phrase(tx))
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
        _authorize(monkeypatch, _phrase(tx_b64))
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
    _authorize(monkeypatch, _phrase(tx))
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
    _authorize(monkeypatch, _phrase(tx))
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
    _authorize(monkeypatch, _phrase(tx))
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
        assert await runner2.place(sol=_SOL) == EXIT_BLOCKED
        assert _submissions(m) == []
        assert _posts_to(m, _QUOTE_RE) == []  # never even quoted
        # The durable execution row now blocks BEFORE the ledger scan runs, so
        # the fresh runner does not even need the network to know it is stuck.
        assert list(m.requests) == []
    recovery = _step(_steps_for(tmp_path, runner2), "execution_recovery")
    assert recovery["blockers"] == 1
    assert recovery["executions"][0]["state"] == "submission_unknown"
    assert recovery["executions"][0]["disposition"] == "resolve"
    assert recovery["executions"][0]["action"] == "blocks"
    capsys.readouterr()
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
        _mock_resolver_pool_rpc(m)
        # Startup reconciliation: two absent sweeps past the expiry height.
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m, pool_probe=False)
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
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m)
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_LAST_VALID + 50))
        assert await runner.place(sol=_SOL) == EXIT_BLOCKED
        assert _submissions(m) == []
    row = await _row(db, decision_id)
    assert row["status"] == "open"
    await session.close()
    await db.close()


async def test_a_row_without_a_signature_is_provably_unsubmitted(tmp_path, monkeypatch):
    """*** Required property 7, the crash-before-signing half. ***

    Submission is reachable only after the signature is committed and read
    back, so a row still holding NULL is one where the run ended before
    signing — a refusal, a Ctrl+C at the prompt, a crash. No transaction
    exists, so the row is retired and the lane clears rather than stalling
    on a row that asserts nothing.
    """
    tx = build_swap_tx().tx_b64
    decision_id = "6d1b345e-2821-40e2-ad83-4ecb18a06879"
    runner, db, session = await _make_runner(tmp_path)
    await _seed_solana_row(db, decision_id=decision_id, signature=None)
    _authorize(monkeypatch, None)  # stop at the approval boundary

    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        # The lane clears, so the run proceeds all the way to the prompt.
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []
        # Nothing was asked of the cluster ABOUT the stale row: there is no
        # signature to ask about, and none is needed to know the answer.

    reconciliation = _step(_steps(tmp_path), "startup_reconciliation")
    assert reconciliation["blocker_count"] == 0
    stale = reconciliation["rows"][0]
    assert stale["resolution"]["verdict"] == "definitively_not_submitted"
    assert stale["resolution"]["expiry_source"] == "never_signed"
    assert stale["auto_retired"] is True
    assert (await _row(db, decision_id))["status"] == "rejected"
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
    assert solana_lane.row_age_seconds(None) is None
    assert solana_lane.row_age_seconds("") is None
    assert solana_lane.row_age_seconds("not-a-timestamp") is None
    # Naive timestamps are of unknown origin — every writer here stamps UTC.
    assert solana_lane.row_age_seconds("2026-07-31T12:00:00") is None
    aged = (now - timedelta(hours=2)).isoformat()
    assert solana_lane.row_age_seconds(aged, now=now) == pytest.approx(7200, abs=2)


def test_age_policy_regimes(tmp_path):
    """The three regimes, and the two that must never clear a row."""
    settings = _settings(tmp_path)
    absent = _report("unresolved")

    # Too young: age proves nothing, so the resolver's answer stands.
    young = solana_lane.apply_age_policy(
        absent, age_seconds=600, settings=settings, had_evidence_height=False
    )
    assert young.verdict == "unresolved"
    assert young.expiry_source is None

    # Inside the window: age alone establishes expiry.
    inside = solana_lane.apply_age_policy(
        absent, age_seconds=7200, settings=settings, had_evidence_height=False
    )
    assert inside.verdict == "definitively_not_submitted"
    assert inside.expiry_source == "row_age"
    assert inside.clears_the_lane is True

    # Past the history window: absence stops being evidence.
    stale = solana_lane.apply_age_policy(
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
        solana_lane.apply_age_policy(
            proven, age_seconds=200_000, settings=settings, had_evidence_height=True
        ).reason
        == "history_window_exceeded"
    )

    # A recorded height is the fresher, more specific fact; age must not
    # overrule it when it says the transaction can still land.
    assert (
        solana_lane.apply_age_policy(
            absent, age_seconds=7200, settings=settings, had_evidence_height=True
        ).verdict
        == "unresolved"
    )

    # Presence is trustworthy at any age — the window bounds what ABSENCE
    # means, not what presence means.
    for verdict in ("landed", "failed_on_chain"):
        carried = solana_lane.apply_age_policy(
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
        solana_lane.apply_age_policy(
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
    assert solana_lane.read_persisted_intent(tmp_path / "evidence", decision_id) is None

    with aioresponses() as m:
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m, rpc_url=_ROUND_ROBIN_RPC_URL)
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
        _mock_resolver_pool_rpc(m)
        m.post(_RPC_URL, payload=_sig_status(known=False))
        m.post(_RPC_URL, payload=_rpc(_HEIGHT_FRESH))
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m, pool_probe=False)
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
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m)
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
        _mock_resolver_pool_rpc(m)
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
    assert "SOLANA LANE STATUS" in out
    assert "keypair custody          : ok" in out
    assert "place would auto-retire this" in out
    # Read-only means read-only: status did NOT retire it.
    assert (await _row(db, decision_id))["status"] == "open"
    await session.close()
    await db.close()


async def test_status_reports_a_refused_keypair_without_crashing(tmp_path, capsys):
    runner, db, session = await _make_runner(tmp_path, custody_ok=False)
    with aioresponses():
        assert await runner.status() == EXIT_OK
    out = capsys.readouterr().out
    assert "keypair custody          : REFUSED" in out
    # Custody was judged from stat, so the key file was never opened.
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def test_status_reports_a_dead_resolver_endpoint_without_crashing(
    tmp_path, capsys
):
    """A dead endpoint is exactly what status is run to discover.

    A read-only report that crashes on the failure it exists to surface tells
    the operator nothing, and `status` is also the command reached for when
    something already looks wrong.
    """
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_RESOLVER_RPC_URLS=f"{_RPC_URL},https://second-node.example/rpc"
    )
    with aioresponses() as m:
        # Neither endpoint answers anything.
        _mock_resolver_pool_rpc(m, genesis=_DEVNET_GENESIS)
        assert await runner.status() == EXIT_OK

    out = capsys.readouterr().out
    assert "UNUSABLE" in out
    assert "corroboration            : available" in out  # configured, not proven
    await session.close()
    await db.close()


def _write_keyfile(tmp_path, keypair) -> str:
    """A real Solana id.json: 32 secret bytes then the 32-byte public key."""
    path = tmp_path / "id.json"
    path.write_text(json.dumps(list(bytes(keypair))), encoding="utf-8")
    return str(path)


async def test_status_never_constructs_a_signer(tmp_path, capsys, monkeypatch):
    """*** `status` reads state; it must not hold signing capability. ***

    It cannot sign today either way — but it was the one command that read
    funded key material with no authorization at all, which reads badly for
    a boundary whose whole point is that the key is not touched until you
    approve. Custody now comes from stat, and the file is checked only via
    its PUBLIC half, so no Keypair is built anywhere on this path.

    Spied at the construction site rather than at the file read, because
    reading a file and holding a signer are different capabilities and it is
    the second one that matters here.
    """
    import scout.live.solana.signer as signer_module

    class _ExplodingKeypair:
        @staticmethod
        def from_bytes(*_args, **_kwargs):
            raise AssertionError("status constructed a signer")

    monkeypatch.setattr(signer_module, "Keypair", _ExplodingKeypair)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("status called load_keypair")

    monkeypatch.setattr(solana_lane, "load_keypair", _forbidden)

    keyfile = _write_keyfile(tmp_path, PAYER)
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_PILOT_KEYPAIR_PATH=keyfile
    )
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": 1}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        assert await runner.status() == EXIT_OK

    out = capsys.readouterr().out
    assert "keypair custody          : ok (mode and owner from stat)" in out
    assert f"declared signer          : {PAYER_PUBKEY}" in out
    assert "matches the declared signer" in out
    assert "no signer constructed" in out
    # The injected loader is the runner's only route to a signer.
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def test_status_flags_a_key_file_that_is_not_the_declared_wallet(
    tmp_path, capsys
):
    """A wrong key file is worth knowing about BEFORE trade day."""
    keyfile = _write_keyfile(tmp_path, OTHER)
    runner, db, session = await _make_runner(
        tmp_path, SOLANA_PILOT_KEYPAIR_PATH=keyfile
    )
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": 1}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        assert await runner.status() == EXIT_OK
    out = capsys.readouterr().out
    assert "key file public half     : MISMATCH" in out
    assert "`place` will refuse at signing" in out
    assert runner.loader_spy.calls == 0
    await session.close()
    await db.close()


async def test_a_signer_mismatch_retires_its_own_row(tmp_path, monkeypatch):
    """The refusal is terminal now, not on some later run.

    Every other refusal path leaves its row terminal on its own. Leaning on
    a subsequent run's unsigned-row sweep would make this one the exception,
    and that sweep is itself an inference from ordering rather than a
    directly observed fact.
    """
    tx = build_swap_tx().tx_b64
    _authorize(monkeypatch, _phrase(tx))
    # The key file decodes to a DIFFERENT wallet than the one this run was
    # built and authorized for.
    runner, db, session = await _make_runner(tmp_path, keypair=OTHER)
    with aioresponses() as m:
        _mock_quote_and_build(m, tx)
        _mock_pre_approval_rpc(m)
        _mock_post_approval_rpc(m)
        assert await runner.place(sol=_SOL) == EXIT_REFUSED
        assert _submissions(m) == []

    abort = _step(_steps(tmp_path), "aborted")
    assert abort["stage"] == "funded_signer"
    assert "was built and authorized for" in abort["reason"]
    # Terminal immediately — no second run required.
    assert await _live_rows(db) == [("rejected", None)]
    assert _step(_steps(tmp_path), "intent_retired")["ledger_status"] == "rejected"
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
    _authorize(monkeypatch, _phrase(tx))
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
    assert custody["declared_signer_pubkey"] == PAYER_PUBKEY
    assert custody["key_material_read"] is False
    await session.close()
    await db.close()


async def test_evidence_survives_a_crash_mid_run(tmp_path):
    """Every completed step is fsynced before the next one starts."""
    runner, db, session = await _make_runner(tmp_path)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash after the quote")

    with aioresponses() as m:
        _mock_resolver_pool_rpc(m)
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
    scrubbed = solana_lane._scrub(
        {
            "keypair_path": "/srv/id.json",
            "API-Key": "xyz",
            "nested": {"private_key": "s3cr3t"},
            "signal_type": "solana_lane",
            "expected_signature": "5xY",
            "signer_pubkey": "abc",
            "raw": b"\x00\x01\x02",
        }
    )
    assert scrubbed["keypair_path"] == "[REDACTED]"
    assert scrubbed["API-Key"] == "[REDACTED]"
    assert scrubbed["nested"]["private_key"] == "[REDACTED]"
    # Public identifiers must survive — they are the whole audit trail.
    assert scrubbed["signal_type"] == "solana_lane"
    assert scrubbed["expected_signature"] == "5xY"
    assert scrubbed["signer_pubkey"] == "abc"
    assert scrubbed["raw"] == "<3 bytes>"


def test_persisted_intent_reads_back_and_fails_closed(tmp_path):
    path = tmp_path / "evidence"
    assert solana_lane.read_persisted_intent(path, "nope") is None
    path.mkdir()
    target = path / "solana_lane_abc.json"
    target.write_text('{"step": "run_started"}\nnot json\n', encoding="utf-8")
    assert solana_lane.read_persisted_intent(path, "abc") is None
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"step": "intent_persisted", "last_valid_block_height": 42})
            + "\n"
        )
    assert (
        solana_lane.read_persisted_intent(path, "abc")["last_valid_block_height"] == 42
    )


# ======================================================================
# Ledger anchor
# ======================================================================
async def test_lane_anchor_is_created_once_and_hidden_from_paper_readers(tmp_path):
    runner, db, session = await _make_runner(tmp_path)
    first = await solana_lane.ensure_lane_anchor(db)
    second = await solana_lane.ensure_lane_anchor(db)
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
    anchor = await solana_lane.ensure_lane_anchor(db)
    row_id = await solana_lane.record_solana_intent(
        db,
        decision_id="6d1b345e-2821-40e2-ad83-4ecb18a0687e",
        paper_trade_id=anchor,
        size_usd="8.52",
        sol_price_usdc="170.5",
    )
    assert row_id > 0
    row = await _row(db, "6d1b345e-2821-40e2-ad83-4ecb18a0687e")
    # The row is written BEFORE the approval prompt, when no signature can
    # exist yet. NULL here is what makes 'provably never submitted' readable
    # straight off the ledger.
    assert (row["status"], row["entry_order_id"]) == ("open", None)

    # ...and the signature arrives later, confirmed by read-back.
    stored = await solana_lane.persist_expected_signature(db, row_id, "5sig")
    assert stored == "5sig"
    row = await _row(db, "6d1b345e-2821-40e2-ad83-4ecb18a0687e")
    assert row["entry_order_id"] == "5sig"
    await session.close()
    await db.close()


# ======================================================================
# Lock + main() wiring
# ======================================================================
def test_lane_lock_is_exclusive_and_never_broken_automatically(tmp_path):
    import os

    db_path = tmp_path / "lane.db"
    fd, path = solana_lane.acquire_lane_lock(db_path)
    try:
        with pytest.raises(LaneLockHeld) as excinfo:
            solana_lane.acquire_lane_lock(db_path)
        # The holder's PID is named so the operator knows what to look for;
        # the lock is never broken for them.
        assert str(os.getpid()) in excinfo.value.holder
        assert path.exists()
    finally:
        solana_lane.release_lane_lock(fd, path)
    assert not path.exists()


def test_the_solana_lock_is_not_the_kraken_lock(tmp_path):
    """Two lanes, two assets, two venues — neither bounds the other."""
    from scout.live import kraken_pilot

    db_path = tmp_path / "lane.db"
    assert solana_lane.lane_lock_path(db_path) != kraken_pilot.pilot_lock_path(db_path)


async def test_main_refuses_when_the_database_does_not_exist(
    tmp_path, monkeypatch, capsys
):
    """sqlite3 creates a DB for any path, so the wrong directory is silent."""
    missing = tmp_path / "nowhere" / "scout.db"
    monkeypatch.setattr(
        solana_lane, "load_settings", lambda: _settings(tmp_path, DB_PATH=str(missing))
    )
    assert await solana_lane.main(["status"]) == EXIT_REFUSED
    assert str(missing.resolve()) in capsys.readouterr().out
    assert not missing.exists()


async def test_main_blocks_place_but_not_status_while_the_lock_is_held(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "lane.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()
    settings = _settings(tmp_path, DB_PATH=str(db_path))
    monkeypatch.setattr(solana_lane, "load_settings", lambda: settings)
    solana_lane.lane_lock_path(db_path).write_text("pid=4242 acquired_at=now\n")

    with aioresponses() as m:
        assert await solana_lane.main(["place", "--sol", "0.05"]) == EXIT_BLOCKED
        assert list(m.requests) == []  # refused before any venue was touched
    out = capsys.readouterr().out
    assert "4242" in out and "Two runs are two swaps" in out

    # status still works — it is how the operator investigates the stale lock.
    with aioresponses() as m:
        m.post(_RPC_URL, payload=_rpc({"context": {"slot": 1}, "value": 1}))
        m.post(_RPC_URL, payload=_token_accounts(0))
        assert await solana_lane.main(["status"]) == EXIT_OK
    assert solana_lane.lane_lock_path(db_path).exists()  # untouched
    solana_lane.lane_lock_path(db_path).unlink()


async def test_main_releases_the_lock_after_a_run(tmp_path, monkeypatch):
    db_path = tmp_path / "lane.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()
    settings = _settings(tmp_path, DB_PATH=str(db_path), SOLANA_MODE="DISABLED")
    monkeypatch.setattr(solana_lane, "load_settings", lambda: settings)
    with aioresponses():
        assert await solana_lane.main(["place", "--sol", "0.05"]) == EXIT_REFUSED
    assert not solana_lane.lane_lock_path(db_path).exists()


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
    assert solana_lane.lamports_from_sol(args.sol) == _LAMPORTS


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
    that only showed up as "the lane does not work" on trade day.
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


def test_solana_lane_defaults_are_safe():
    s = Settings(_env_file=None, **_REQUIRED)
    assert s.SOLANA_MODE == "DISABLED"
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
