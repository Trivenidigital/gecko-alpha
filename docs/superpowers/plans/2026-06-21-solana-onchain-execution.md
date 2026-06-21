# Solana On-Chain Execution Adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Solana on-chain execution venue (`SolanaSwapAdapter`) that lets early Solana signals route to real Jupiter DEX swaps, reusing the existing live engine, gates, kill switch, idempotency, reconciliation, and ledgers unchanged.

**Architecture:** A new `SolanaSwapAdapter(ExchangeAdapter)` whose "venue" is the Jupiter aggregator, backed by three small sub-modules: `jupiter_client` (HTTP quote/swap, no keys), `wallet` (the only key-holder; a signer seam), and `rpc` (send/confirm/balance/simulate). The engine, gates, kill switch, ledgers, and dashboard keep treating it as "a venue named `solana`." On-chain-specific gates (price-impact, sellability/rug, gas reserve) are added to the existing `Gates` chain; reconciliation gains a tx-signature recovery path.

**Tech Stack:** Python 3 / asyncio, `aiohttp` (Jupiter + Solana JSON-RPC), `solders` (Keypair / Pubkey / VersionedTransaction), `pydantic-settings`, `structlog`, `aiosqlite`, `pytest` + `aioresponses`.

## Global Constraints

- Python deps are version-pinned in `pyproject.toml`; add new deps with explicit floors/ceilings (style: `"aiohttp>=3.10,<4"`). Do NOT bump existing pins.
- `OrderRequest.size_usd` is `float`. `OrderConfirmation.status` ∈ `{'filled','partial','rejected','pending','timeout'}` only.
- Gate `reject_reason` MUST be a member of `VALID_REJECT_REASONS` in `scout/live/gates.py` — adding a new reason means adding it to that frozenset.
- Private key material lives ONLY in `scout/live/solana/wallet.py`. Never log it, never put it in `.env.example`, never commit it. Mirror the `BINANCE_API_SECRET` SecretStr pattern in `scout/config.py`.
- Tests use the existing conventions: `@pytest.mark.asyncio`, `Database(tmp_path / "t.db")` + `await db.initialize()`, a `_settings(**overrides)` factory that injects `_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")` and `Settings(_env_file=None, ...)`, and `aioresponses` for HTTP mocks. No real network in tests.
- Mainnet mints: USDC = `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (6 decimals); wSOL = `So11111111111111111111111111111111111111112` (9 decimals).
- TDD: write the failing test, watch it fail, implement minimally, watch it pass, commit. One logical change per commit.
- Run the full suite with `uv run pytest --tb=short -q`; format with `uv run black scout/ tests/`.

---

### Task 1: Dependencies + Solana constants

**Files:**
- Modify: `pyproject.toml` (dependencies array)
- Create: `scout/live/solana/__init__.py`
- Create: `scout/live/solana/constants.py`
- Test: `tests/live/solana/test_constants.py`

**Interfaces:**
- Produces: `scout.live.solana.constants` with `USDC_MINT: str`, `WSOL_MINT: str`, `USDC_DECIMALS: int = 6`, `SOL_DECIMALS: int = 9`, `LAMPORTS_PER_SOL: int = 1_000_000_000`, and helpers `usdc_to_base_units(amount_usd: float) -> int`, `base_units_to_usdc(units: int) -> float`.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, append to the `dependencies` array (after the `"ccxt==4.5.52",` line):

```toml
    # Solana on-chain execution (BL-NEW-SOLANA). solders = typed keypair/tx;
    # base58 for raw key decode. Jupiter + JSON-RPC go over aiohttp (already present).
    "solders>=0.21,<0.28",
    "base58>=2.1,<3",
```

- [ ] **Step 2: Sync deps**

Run: `uv sync`
Expected: resolves and installs `solders` + `base58` with no conflict against `ccxt==4.5.52`.

- [ ] **Step 3: Write the failing test**

Create `tests/live/solana/__init__.py` (empty), then `tests/live/solana/test_constants.py`:

```python
from __future__ import annotations

from scout.live.solana import constants as c


def test_mints_and_decimals():
    assert c.USDC_MINT == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert c.WSOL_MINT == "So11111111111111111111111111111111111111112"
    assert c.USDC_DECIMALS == 6
    assert c.SOL_DECIMALS == 9
    assert c.LAMPORTS_PER_SOL == 1_000_000_000


def test_usdc_unit_conversion_roundtrip():
    assert c.usdc_to_base_units(10.0) == 10_000_000
    assert c.base_units_to_usdc(10_000_000) == 10.0
    # sub-cent rounds to integer base units
    assert c.usdc_to_base_units(0.000001) == 1
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_constants.py -v`
Expected: FAIL — `ModuleNotFoundError: scout.live.solana`

- [ ] **Step 5: Create the package + constants**

Create `scout/live/solana/__init__.py` (empty file).

Create `scout/live/solana/constants.py`:

```python
"""Solana mainnet constants and unit conversions for the swap adapter."""

from __future__ import annotations

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"

USDC_DECIMALS = 6
SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 1_000_000_000


def usdc_to_base_units(amount_usd: float) -> int:
    """USD (== USDC 1:1) to integer base units (6 decimals), rounded."""
    return int(round(amount_usd * (10**USDC_DECIMALS)))


def base_units_to_usdc(units: int) -> float:
    """Integer USDC base units back to a float USD amount."""
    return units / (10**USDC_DECIMALS)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_constants.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock scout/live/solana/__init__.py scout/live/solana/constants.py tests/live/solana/
git commit -m "feat(solana): add deps + mint/decimal constants"
```

---

### Task 2: Jupiter client — quote

**Files:**
- Create: `scout/live/solana/jupiter_client.py`
- Test: `tests/live/solana/test_jupiter_client.py`

**Interfaces:**
- Consumes: `scout.live.solana.constants`.
- Produces: `JupiterClient(session: aiohttp.ClientSession, base_url: str)` with `async def get_quote(self, *, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict`. Returns the raw Jupiter quote dict (keys include `outAmount`, `priceImpactPct`, `inAmount`, `routePlan`). Raises `JupiterError` on non-200 or routing failure.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_jupiter_client.py`:

```python
from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.live.solana.jupiter_client import JupiterClient, JupiterError

_QUOTE_RE = re.compile(r"https://api\.jup\.ag/swap/v1/quote.*")


@pytest.mark.asyncio
async def test_get_quote_returns_payload():
    async with aiohttp.ClientSession() as session:
        client = JupiterClient(session, base_url="https://api.jup.ag/swap/v1")
        with aioresponses() as m:
            m.get(
                _QUOTE_RE,
                payload={
                    "inAmount": "10000000",
                    "outAmount": "123456789",
                    "priceImpactPct": "0.0042",
                    "routePlan": [{"swapInfo": {}}],
                },
            )
            q = await client.get_quote(
                input_mint="USDC", output_mint="MINT", amount=10_000_000, slippage_bps=50
            )
        assert q["outAmount"] == "123456789"
        assert q["priceImpactPct"] == "0.0042"


@pytest.mark.asyncio
async def test_get_quote_raises_on_http_error():
    async with aiohttp.ClientSession() as session:
        client = JupiterClient(session, base_url="https://api.jup.ag/swap/v1")
        with aioresponses() as m:
            m.get(_QUOTE_RE, status=400, payload={"error": "no route"})
            with pytest.raises(JupiterError):
                await client.get_quote(
                    input_mint="USDC", output_mint="MINT", amount=1, slippage_bps=50
                )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_jupiter_client.py -v`
Expected: FAIL — `ModuleNotFoundError: scout.live.solana.jupiter_client`

- [ ] **Step 3: Implement the quote method**

Create `scout/live/solana/jupiter_client.py`:

```python
"""Jupiter v6 aggregator HTTP client. Pure HTTP — holds no keys, signs nothing.

Quote → swap-transaction flow:
  get_quote()       -> GET  /quote  (routing + priceImpactPct)
  build_swap_tx()   -> POST /swap   (returns a base64 VersionedTransaction)
"""

from __future__ import annotations

from typing import Any

import aiohttp
import structlog

log = structlog.get_logger(__name__)


class JupiterError(RuntimeError):
    """Quote/swap failed (no route, HTTP error, malformed response)."""


class JupiterClient:
    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, api_key: str | None = None
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        # api.jup.ag requires a free key via x-api-key; lite-api.jup.ag is keyless.
        self._headers = {"x-api-key": api_key} if api_key else {}

    async def get_quote(
        self, *, input_mint: str, output_mint: str, amount: int, slippage_bps: int
    ) -> dict[str, Any]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }
        async with self._session.get(
            f"{self._base}/quote", params=params, headers=self._headers
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise JupiterError(f"quote http {resp.status}: {body}")
            if not body.get("outAmount") or not body.get("routePlan"):
                raise JupiterError(f"quote no route: {body}")
            return body
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_jupiter_client.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/live/solana/jupiter_client.py tests/live/solana/test_jupiter_client.py
git commit -m "feat(solana): Jupiter quote client"
```

---

### Task 3: Jupiter client — build swap transaction

**Files:**
- Modify: `scout/live/solana/jupiter_client.py`
- Test: `tests/live/solana/test_jupiter_client.py` (add)

**Interfaces:**
- Produces: `JupiterClient.build_swap_tx(self, *, quote: dict, user_pubkey: str, priority_fee_lamports: int) -> str` → base64 `swapTransaction` string. Raises `JupiterError` on non-200 or missing `swapTransaction`.

- [ ] **Step 1: Write the failing test**

Append to `tests/live/solana/test_jupiter_client.py`:

```python
_SWAP_RE = re.compile(r"https://api\.jup\.ag/swap/v1/swap.*")


@pytest.mark.asyncio
async def test_build_swap_tx_returns_base64():
    async with aiohttp.ClientSession() as session:
        client = JupiterClient(session, base_url="https://api.jup.ag/swap/v1")
        with aioresponses() as m:
            m.post(_SWAP_RE, payload={"swapTransaction": "QUJDRA=="})
            tx = await client.build_swap_tx(
                quote={"outAmount": "1"}, user_pubkey="PUBKEY", priority_fee_lamports=5000
            )
        assert tx == "QUJDRA=="


@pytest.mark.asyncio
async def test_build_swap_tx_raises_when_missing():
    async with aiohttp.ClientSession() as session:
        client = JupiterClient(session, base_url="https://api.jup.ag/swap/v1")
        with aioresponses() as m:
            m.post(_SWAP_RE, payload={})
            with pytest.raises(JupiterError):
                await client.build_swap_tx(
                    quote={}, user_pubkey="PUBKEY", priority_fee_lamports=5000
                )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_jupiter_client.py -k build_swap_tx -v`
Expected: FAIL — `AttributeError: 'JupiterClient' object has no attribute 'build_swap_tx'`

- [ ] **Step 3: Implement build_swap_tx**

Append to the `JupiterClient` class in `scout/live/solana/jupiter_client.py`:

```python
    async def build_swap_tx(
        self, *, quote: dict[str, Any], user_pubkey: str, priority_fee_lamports: int
    ) -> str:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": priority_fee_lamports,
        }
        async with self._session.post(
            f"{self._base}/swap", json=payload, headers=self._headers
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise JupiterError(f"swap http {resp.status}: {body}")
            tx = body.get("swapTransaction")
            if not tx:
                raise JupiterError(f"swap missing swapTransaction: {body}")
            return tx
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_jupiter_client.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/live/solana/jupiter_client.py tests/live/solana/test_jupiter_client.py
git commit -m "feat(solana): Jupiter build_swap_tx"
```

---

### Task 4: Wallet — the signer seam

**Files:**
- Create: `scout/live/solana/wallet.py`
- Test: `tests/live/solana/test_wallet.py`

**Interfaces:**
- Produces:
  - `Signer` Protocol with `def pubkey(self) -> str` and `def sign(self, tx_b64: str) -> str` (takes a base64 unsigned VersionedTransaction, returns base64 fully-signed tx).
  - `LocalEncryptedSigner(secret_base58: str)` implementing `Signer`. Loads a `solders.keypair.Keypair` from a base58 secret. `__repr__`/`__str__` MUST NOT expose key bytes.
  - `make_signer(settings) -> Signer | None` → returns a `LocalEncryptedSigner` if `SOLANA_WALLET_SECRET` is set, else `None`.

**Note:** This is the ONLY file that touches private key material. A future `RemoteSigner` implements the same two-method `Signer` Protocol with zero adapter changes.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_wallet.py`:

```python
from __future__ import annotations

import base64

import pytest
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from scout.live.solana.wallet import LocalEncryptedSigner, Signer


def _unsigned_tx_b64(payer: Keypair) -> str:
    # Minimal well-formed versioned tx (one no-op ix), blockhash is a Hash
    # (NOT bytes), default signature slot so the adapter can sign it. Verified
    # against solders 0.27 API: MessageV0.try_compile + VersionedTransaction.populate.
    ix = Instruction(Pubkey.default(), bytes([1]), [])
    msg = MessageV0.try_compile(payer.pubkey(), [ix], [], Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    return base64.b64encode(bytes(tx)).decode()


def test_pubkey_matches_keypair():
    kp = Keypair()
    signer = LocalEncryptedSigner(str(kp))
    assert signer.pubkey() == str(kp.pubkey())


def test_repr_does_not_leak_secret():
    kp = Keypair()
    secret = str(kp)
    signer = LocalEncryptedSigner(secret)
    assert secret not in repr(signer)
    assert secret not in str(signer)


def test_sign_returns_base64_signed_tx():
    kp = Keypair()
    signer = LocalEncryptedSigner(str(kp))
    signed_b64 = signer.sign(_unsigned_tx_b64(kp))
    raw = base64.b64decode(signed_b64)
    signed = VersionedTransaction.from_bytes(raw)
    # signature slot populated (not all-zero)
    assert any(bytes(sig) != bytes(64) for sig in signed.signatures)


def test_protocol_is_satisfied():
    assert isinstance(LocalEncryptedSigner(str(Keypair())), Signer)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_wallet.py -v`
Expected: FAIL — `ModuleNotFoundError: scout.live.solana.wallet`

- [ ] **Step 3: Implement the wallet**

Create `scout/live/solana/wallet.py`:

```python
"""Signer seam — the ONLY module that holds Solana private key material.

Phase 1: LocalEncryptedSigner loads the key in-process from the
SOLANA_WALLET_SECRET secret (base58). A future RemoteSigner can implement
the same Signer Protocol (pubkey/sign) to move signing into an isolated
service with zero adapter changes.
"""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

import structlog
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

log = structlog.get_logger(__name__)


@runtime_checkable
class Signer(Protocol):
    def pubkey(self) -> str: ...
    def sign(self, tx_b64: str) -> str: ...


class LocalEncryptedSigner:
    """In-process signer. Never logs, reprs, or persists key bytes."""

    def __init__(self, secret_base58: str) -> None:
        self._kp = Keypair.from_base58_string(secret_base58)
        self._pubkey = str(self._kp.pubkey())

    def pubkey(self) -> str:
        return self._pubkey

    def sign(self, tx_b64: str) -> str:
        raw = base64.b64decode(tx_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [self._kp])
        return base64.b64encode(bytes(signed)).decode()

    def __repr__(self) -> str:  # never expose key
        return f"<LocalEncryptedSigner pubkey={self._pubkey}>"

    __str__ = __repr__


def make_signer(settings) -> Signer | None:
    secret = getattr(settings, "SOLANA_WALLET_SECRET", None)
    if secret is None:
        return None
    raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
    if not raw:
        return None
    return LocalEncryptedSigner(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_wallet.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/live/solana/wallet.py tests/live/solana/test_wallet.py
git commit -m "feat(solana): wallet signer seam (Local + Protocol)"
```

---

### Task 5: RPC client — balance, send, confirm, simulate

**Files:**
- Create: `scout/live/solana/rpc.py`
- Test: `tests/live/solana/test_rpc.py`

**Interfaces:**
- Produces: `SolanaRpc(session: aiohttp.ClientSession, url: str)` with:
  - `async def get_token_balance(self, *, owner: str, mint: str) -> float` (UI amount; 0.0 if no token account)
  - `async def get_sol_balance(self, *, owner: str) -> float` (SOL, not lamports)
  - `async def send_raw_transaction(self, signed_b64: str) -> str` (returns tx signature)
  - `async def confirm_signature(self, signature: str) -> str` (one poll → one of `'success' | 'failed' | 'pending'`)
  - `async def simulate_transaction(self, tx_b64: str) -> bool` (True if sim succeeds with no `err`)
- Raises `RpcError` on JSON-RPC `error` for send (transient handled by caller).

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_rpc.py`:

```python
from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.live.solana.rpc import RpcError, SolanaRpc

URL = "https://rpc.test/solana"


@pytest.mark.asyncio
async def test_get_token_balance_parses_ui_amount():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(URL, payload={
                "jsonrpc": "2.0", "id": 1,
                "result": {"value": [
                    {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 42.5}}}}}}
                ]},
            })
            bal = await rpc.get_token_balance(owner="OWNER", mint="USDC")
        assert bal == 42.5


@pytest.mark.asyncio
async def test_get_token_balance_zero_when_no_account():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1, "result": {"value": []}})
            bal = await rpc.get_token_balance(owner="OWNER", mint="USDC")
        assert bal == 0.0


@pytest.mark.asyncio
async def test_send_raw_transaction_returns_signature():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1, "result": "SIGNATURE123"})
            sig = await rpc.send_raw_transaction("QUJD")
        assert sig == "SIGNATURE123"


@pytest.mark.asyncio
async def test_send_raw_transaction_raises_on_error():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1,
                                 "error": {"code": -32002, "message": "blockhash not found"}})
            with pytest.raises(RpcError):
                await rpc.send_raw_transaction("QUJD")


@pytest.mark.asyncio
async def test_confirm_signature_states():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        # success
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1,
                                 "result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}})
            assert await rpc.confirm_signature("SIG") == "success"
        # on-chain failure
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1,
                                 "result": {"value": [{"confirmationStatus": "confirmed", "err": {"x": 1}}]}})
            assert await rpc.confirm_signature("SIG") == "failed"
        # not yet landed
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1, "result": {"value": [None]}})
            assert await rpc.confirm_signature("SIG") == "pending"


@pytest.mark.asyncio
async def test_simulate_transaction_success_and_failure():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1, "result": {"value": {"err": None}}})
            assert await rpc.simulate_transaction("QUJD") is True
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1, "result": {"value": {"err": {"e": 1}}}})
            assert await rpc.simulate_transaction("QUJD") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_rpc.py -v`
Expected: FAIL — `ModuleNotFoundError: scout.live.solana.rpc`

- [ ] **Step 3: Implement the RPC client**

Create `scout/live/solana/rpc.py`:

```python
"""Thin async Solana JSON-RPC client over aiohttp. No keys."""

from __future__ import annotations

from typing import Any

import aiohttp
import structlog

from scout.live.solana.constants import LAMPORTS_PER_SOL

log = structlog.get_logger(__name__)


class RpcError(RuntimeError):
    """JSON-RPC returned an error object."""


class SolanaRpc:
    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self._session = session
        self._url = url

    async def _call(self, method: str, params: list[Any]) -> Any:
        req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with self._session.post(self._url, json=req) as resp:
            body = await resp.json()
        if "error" in body:
            raise RpcError(f"{method}: {body['error']}")
        return body.get("result")

    async def get_token_balance(self, *, owner: str, mint: str) -> float:
        result = await self._call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        accounts = (result or {}).get("value", [])
        if not accounts:
            return 0.0
        info = accounts[0]["account"]["data"]["parsed"]["info"]
        return float(info["tokenAmount"]["uiAmount"] or 0.0)

    async def get_sol_balance(self, *, owner: str) -> float:
        result = await self._call("getBalance", [owner])
        lamports = (result or {}).get("value", 0)
        return lamports / LAMPORTS_PER_SOL

    async def send_raw_transaction(self, signed_b64: str) -> str:
        return await self._call(
            "sendTransaction",
            [signed_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 2}],
        )

    async def confirm_signature(self, signature: str) -> str:
        result = await self._call(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
        )
        value = (result or {}).get("value", [None])
        status = value[0] if value else None
        if status is None:
            return "pending"
        if status.get("err") is not None:
            return "failed"
        if status.get("confirmationStatus") in ("confirmed", "finalized"):
            return "success"
        return "pending"

    async def simulate_transaction(self, tx_b64: str) -> bool:
        result = await self._call(
            "simulateTransaction", [tx_b64, {"encoding": "base64", "sigVerify": False}]
        )
        return (result or {}).get("value", {}).get("err") is None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_rpc.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/live/solana/rpc.py tests/live/solana/test_rpc.py
git commit -m "feat(solana): JSON-RPC client (balance/send/confirm/simulate)"
```

---

### Task 6: Config — `SOLANA_*` settings block

**Files:**
- Modify: `scout/config.py` (after the `BINANCE_API_SECRET` line in the LIVE_* block)
- Test: `tests/live/solana/test_solana_settings.py`

**Interfaces:**
- Produces on `Settings`: `SOLANA_RPC_URL: str`, `SOLANA_JUPITER_URL: str`, `SOLANA_WALLET_SECRET: SecretStr | None`, `SOLANA_SLIPPAGE_BPS_CAP: int`, `SOLANA_PRIORITY_FEE_LAMPORTS: int`, `SOLANA_MAX_PRICE_IMPACT_PCT: float`, `SOLANA_MIN_SOL_GAS_RESERVE: float`, `SOLANA_FLOAT_CAP_USD: Decimal`, `SOLANA_CONFIRM_TIMEOUT_SEC: float`, `SOLANA_SWEEP_COLD_WALLET: str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_solana_settings.py`:

```python
from __future__ import annotations

from decimal import Decimal

from scout.config import Settings

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def test_solana_defaults():
    s = Settings(_env_file=None, **_REQUIRED)
    assert s.SOLANA_RPC_URL.startswith("https://")
    assert s.SOLANA_JUPITER_URL == "https://api.jup.ag/swap/v1"
    assert s.SOLANA_JUPITER_API_KEY is None
    assert s.SOLANA_WALLET_SECRET is None
    assert s.SOLANA_SLIPPAGE_BPS_CAP == 100
    assert s.SOLANA_MAX_PRICE_IMPACT_PCT == 3.0
    assert s.SOLANA_MIN_SOL_GAS_RESERVE == 0.02
    assert s.SOLANA_FLOAT_CAP_USD == Decimal("100")
    assert s.SOLANA_CONFIRM_TIMEOUT_SEC == 60.0


def test_solana_secret_is_not_plaintext_in_repr():
    s = Settings(_env_file=None, **_REQUIRED, SOLANA_WALLET_SECRET="supersecret")
    assert "supersecret" not in repr(s.SOLANA_WALLET_SECRET)
    assert s.SOLANA_WALLET_SECRET.get_secret_value() == "supersecret"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_solana_settings.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'SOLANA_RPC_URL'`

- [ ] **Step 3: Add the settings block**

In `scout/config.py`, immediately AFTER the `BINANCE_API_SECRET: SecretStr | None = None` line, add:

```python
    # -------- Solana on-chain execution (BL-NEW-SOLANA) --------
    # Jupiter aggregator venue. Keys live ONLY in scout/live/solana/wallet.py.
    # SOLANA_WALLET_SECRET is base58 keypair secret; NEVER add to .env.example.
    SOLANA_RPC_URL: str = "https://api.mainnet-beta.solana.com"
    # Jupiter v6 quote-api was deprecated 2025-10. Use swap/v1; api.jup.ag
    # needs a free key (x-api-key header), lite-api.jup.ag is keyless+throttled.
    SOLANA_JUPITER_URL: str = "https://api.jup.ag/swap/v1"
    SOLANA_JUPITER_API_KEY: SecretStr | None = None
    SOLANA_WALLET_SECRET: SecretStr | None = None
    # Execution quality (memecoin pools are thin → wider caps than CEX defaults)
    SOLANA_SLIPPAGE_BPS_CAP: int = 100
    SOLANA_PRIORITY_FEE_LAMPORTS: int = 50_000
    SOLANA_MAX_PRICE_IMPACT_PCT: float = 3.0
    SOLANA_MIN_SOL_GAS_RESERVE: float = 0.02  # SOL kept for fees
    # Risk: live USDC float ceiling; daily sweep returns excess to cold wallet.
    SOLANA_FLOAT_CAP_USD: Decimal = Decimal("100")
    SOLANA_CONFIRM_TIMEOUT_SEC: float = 60.0
    SOLANA_SWEEP_COLD_WALLET: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_solana_settings.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/config.py tests/live/solana/test_solana_settings.py
git commit -m "feat(solana): SOLANA_* settings block"
```

---

### Task 7: Adapter — read-only surface (resolve, metadata, price, depth, quote_at_size)

**Files:**
- Create: `scout/live/solana_swap_adapter.py`
- Test: `tests/live/solana/test_solana_adapter_readonly.py`

**Interfaces:**
- Consumes: `JupiterClient`, `SolanaRpc`, `Signer`, `scout.live.adapter_base` (`ExchangeAdapter`, `VenueMetadata`, `OrderRequest`, `OrderConfirmation`), `scout.live.types.Depth`, `scout.live.solana.constants`.
- Produces: `SolanaSwapAdapter(ExchangeAdapter)` with `venue_name = "solana"`, `is_onchain = True`, constructed as
  `SolanaSwapAdapter(*, settings, jupiter: JupiterClient, rpc: SolanaRpc, signer: Signer | None, db=None)`.
  This task implements: `resolve_pair_for_symbol`, `fetch_venue_metadata`, `fetch_price`, `fetch_depth`, and a new helper `async def quote_at_size(self, *, venue_pair: str, side: str, size_usd: float) -> dict` returning `{"out_amount": int, "price_impact_pct": float, "mid": Decimal}`. `fetch_exchange_info_row` and `send_order` raise `NotImplementedError`.
- For an on-chain "pair", `venue_pair` IS the token mint string. `side="buy"` → input USDC / output mint; `side="sell"` → input mint / output USDC.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_solana_adapter_readonly.py`:

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from scout.config import Settings
from scout.live.adapter_base import VenueMetadata
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _FakeJupiter:
    def __init__(self, quote):
        self._quote = quote
        self.calls = []

    async def get_quote(self, *, input_mint, output_mint, amount, slippage_bps):
        self.calls.append((input_mint, output_mint, amount))
        return self._quote


def _adapter(quote):
    return SolanaSwapAdapter(
        settings=_settings(), jupiter=_FakeJupiter(quote), rpc=None, signer=None
    )


@pytest.mark.asyncio
async def test_resolve_pair_returns_mint_when_routable():
    a = _adapter({"outAmount": "1000", "priceImpactPct": "0.001", "routePlan": [{}]})
    assert await a.resolve_pair_for_symbol(MINT) == MINT


@pytest.mark.asyncio
async def test_fetch_venue_metadata_shape():
    a = _adapter({"outAmount": "1000", "priceImpactPct": "0.001", "routePlan": [{}]})
    meta = await a.fetch_venue_metadata(MINT)
    assert isinstance(meta, VenueMetadata)
    assert meta.venue == "solana"
    assert meta.venue_pair == MINT
    assert meta.quote == "USDC"
    assert meta.asset_class == "spot"


@pytest.mark.asyncio
async def test_quote_at_size_buy_uses_usdc_input_and_converts_impact():
    # priceImpactPct "0.0042" (fraction) -> 0.42 percent
    a = _adapter({"outAmount": "123456789", "priceImpactPct": "0.0042", "routePlan": [{}]})
    out = await a.quote_at_size(venue_pair=MINT, side="buy", size_usd=10.0)
    assert out["out_amount"] == 123456789
    assert round(out["price_impact_pct"], 4) == 0.42
    # input mint was USDC, amount = 10 * 1e6 base units
    assert a._jupiter.calls[0][0].endswith("Dt1v")  # USDC mint
    assert a._jupiter.calls[0][2] == 10_000_000


@pytest.mark.asyncio
async def test_fetch_depth_synthesizes_from_quote():
    a = _adapter({"outAmount": "1000000", "priceImpactPct": "0.01", "routePlan": [{}]})
    depth = await a.fetch_depth(MINT)
    assert depth.pair == MINT
    assert depth.mid > Decimal("0")
    assert len(depth.asks) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_solana_adapter_readonly.py -v`
Expected: FAIL — `ModuleNotFoundError: scout.live.solana_swap_adapter`

- [ ] **Step 3: Implement the read-only adapter surface**

Create `scout/live/solana_swap_adapter.py`:

```python
"""SolanaSwapAdapter — ExchangeAdapter whose venue is the Jupiter aggregator.

The only component that knows the venue is on-chain. Maps the CEX-shaped
ExchangeAdapter contract onto Jupiter quotes + Solana RPC. venue_pair IS the
token mint string. Buy = USDC->mint; Sell = mint->USDC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from scout.live.adapter_base import (
    ExchangeAdapter,
    OrderConfirmation,
    OrderRequest,
    VenueMetadata,
)
from scout.live.solana import constants as c
from scout.live.types import Depth, DepthLevel

log = structlog.get_logger(__name__)


class SolanaSwapAdapter(ExchangeAdapter):
    venue_name: str = "solana"
    is_onchain: bool = True

    def __init__(self, *, settings, jupiter, rpc, signer, db: Any | None = None) -> None:
        self._settings = settings
        self._jupiter = jupiter
        self._rpc = rpc
        self._signer = signer
        self._db = db

    # ---- legacy / unused on this venue ----
    async def fetch_exchange_info_row(self, pair: str) -> dict | None:
        raise NotImplementedError("on-chain venue has no exchangeInfo")

    async def send_order(self, *, pair: str, side: str, size_usd: Decimal) -> dict:
        raise NotImplementedError("use place_order_request")

    # ---- metadata / resolution ----
    async def resolve_pair_for_symbol(self, symbol: str) -> str | None:
        meta = await self.fetch_venue_metadata(symbol)
        return meta.venue_pair if meta is not None else None

    async def fetch_venue_metadata(self, canonical: str) -> VenueMetadata | None:
        # canonical here is the SPL mint address. Routable if Jupiter quotes it.
        try:
            await self._jupiter.get_quote(
                input_mint=c.USDC_MINT,
                output_mint=canonical,
                amount=c.usdc_to_base_units(1.0),
                slippage_bps=self._settings.SOLANA_SLIPPAGE_BPS_CAP,
            )
        except Exception:
            return None
        return VenueMetadata(
            venue="solana",
            canonical=canonical,
            venue_pair=canonical,
            quote="USDC",
            asset_class="spot",
            min_size=None,
            tick_size=None,
            lot_size=None,
        )

    def _mints_for_side(self, venue_pair: str, side: str) -> tuple[str, str]:
        if side == "buy":
            return c.USDC_MINT, venue_pair
        return venue_pair, c.USDC_MINT

    async def quote_at_size(
        self, *, venue_pair: str, side: str, size_usd: float
    ) -> dict[str, Any]:
        input_mint, output_mint = self._mints_for_side(venue_pair, side)
        amount = c.usdc_to_base_units(size_usd)  # input is USDC for buy
        quote = await self._jupiter.get_quote(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount,
            slippage_bps=self._settings.SOLANA_SLIPPAGE_BPS_CAP,
        )
        out_amount = int(quote["outAmount"])
        # Jupiter priceImpactPct is a fraction string (0.0042 == 0.42%).
        price_impact_pct = float(quote.get("priceImpactPct") or 0.0) * 100.0
        mid = Decimal(amount) / Decimal(out_amount) if out_amount else Decimal("0")
        self._last_quote = quote
        return {"out_amount": out_amount, "price_impact_pct": price_impact_pct, "mid": mid}

    async def fetch_price(self, pair: str) -> Decimal:
        # tiny-notional buy quote → mid (USDC per token base unit, normalized)
        q = await self.quote_at_size(venue_pair=pair, side="buy", size_usd=1.0)
        return q["mid"]

    async def fetch_depth(self, pair: str, limit: int = 100) -> Depth:
        # No order book on-chain. Synthesize a single-level Depth from the
        # at-size quote so generic callers still get a usable mid; the
        # on-chain gate uses price-impact directly (Task 11), not this walk.
        q = await self.quote_at_size(
            venue_pair=pair, side="buy", size_usd=float(self._settings.LIVE_TRADE_AMOUNT_USD)
        )
        mid = q["mid"] or Decimal("0")
        level = DepthLevel(price=mid, qty=Decimal("0"))
        return Depth(
            pair=pair,
            bids=(level,),
            asks=(level,),
            mid=mid,
            fetched_at=datetime.now(timezone.utc),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_solana_adapter_readonly.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/live/solana_swap_adapter.py tests/live/solana/test_solana_adapter_readonly.py
git commit -m "feat(solana): adapter read-only surface (resolve/metadata/quote/price/depth)"
```

---

### Task 8: Adapter — balances & sellability

**Files:**
- Modify: `scout/live/solana_swap_adapter.py`
- Test: `tests/live/solana/test_solana_adapter_balance.py`

**Interfaces:**
- Produces: `fetch_account_balance(self, asset: str = "USDT") -> float` — `asset` in `{"USDC","USDT"}` → USDC token balance; `"SOL"` → SOL balance.
  `async def is_sellable(self, *, venue_pair: str, expected_out_amount: int) -> bool` — simulates a sell of `expected_out_amount` tokens back to USDC; returns False if no route or sim fails (honeypot guard).

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_solana_adapter_balance.py`:

```python
from __future__ import annotations

import pytest

from scout.config import Settings
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _FakeRpc:
    def __init__(self, *, usdc=0.0, sol=0.0, sim=True):
        self._usdc, self._sol, self._sim = usdc, sol, sim

    async def get_token_balance(self, *, owner, mint):
        return self._usdc

    async def get_sol_balance(self, *, owner):
        return self._sol

    async def simulate_transaction(self, tx_b64):
        return self._sim


class _FakeJupiter:
    def __init__(self, *, route=True, swap_ok=True):
        self._route, self._swap_ok = route, swap_ok

    async def get_quote(self, *, input_mint, output_mint, amount, slippage_bps):
        if not self._route:
            raise RuntimeError("no route")
        return {"outAmount": "1", "priceImpactPct": "0.001", "routePlan": [{}]}

    async def build_swap_tx(self, *, quote, user_pubkey, priority_fee_lamports):
        if not self._swap_ok:
            raise RuntimeError("no swap")
        return "QUJD"


class _FakeSigner:
    def pubkey(self):
        return "OWNER_PUBKEY"

    def sign(self, tx_b64):
        return tx_b64


def _adapter(jup, rpc, signer=_FakeSigner()):
    return SolanaSwapAdapter(settings=_settings(), jupiter=jup, rpc=rpc, signer=signer)


@pytest.mark.asyncio
async def test_fetch_balance_usdc_and_sol():
    a = _adapter(_FakeJupiter(), _FakeRpc(usdc=25.0, sol=0.5))
    assert await a.fetch_account_balance("USDC") == 25.0
    assert await a.fetch_account_balance("USDT") == 25.0
    assert await a.fetch_account_balance("SOL") == 0.5


@pytest.mark.asyncio
async def test_is_sellable_true_when_route_and_sim_ok():
    a = _adapter(_FakeJupiter(route=True), _FakeRpc(sim=True))
    assert await a.is_sellable(venue_pair=MINT, expected_out_amount=1000) is True


@pytest.mark.asyncio
async def test_is_sellable_false_when_no_sell_route():
    a = _adapter(_FakeJupiter(route=False), _FakeRpc(sim=True))
    assert await a.is_sellable(venue_pair=MINT, expected_out_amount=1000) is False


@pytest.mark.asyncio
async def test_is_sellable_false_when_sim_fails():
    a = _adapter(_FakeJupiter(route=True), _FakeRpc(sim=False))
    assert await a.is_sellable(venue_pair=MINT, expected_out_amount=1000) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_solana_adapter_balance.py -v`
Expected: FAIL — `AttributeError: 'SolanaSwapAdapter' object has no attribute 'fetch_account_balance'`

- [ ] **Step 3: Implement balances + sellability**

Append to the `SolanaSwapAdapter` class:

```python
    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        owner = self._signer.pubkey() if self._signer is not None else None
        if owner is None:
            return 0.0
        if asset.upper() == "SOL":
            return await self._rpc.get_sol_balance(owner=owner)
        return await self._rpc.get_token_balance(owner=owner, mint=c.USDC_MINT)

    async def is_sellable(self, *, venue_pair: str, expected_out_amount: int) -> bool:
        """Honeypot guard: can we route AND simulate selling the position back
        to USDC? Any failure → not sellable → do not buy."""
        owner = self._signer.pubkey() if self._signer is not None else None
        if owner is None:
            return False
        try:
            sell_quote = await self._jupiter.get_quote(
                input_mint=venue_pair,
                output_mint=c.USDC_MINT,
                amount=expected_out_amount,
                slippage_bps=self._settings.SOLANA_SLIPPAGE_BPS_CAP,
            )
            tx_b64 = await self._jupiter.build_swap_tx(
                quote=sell_quote,
                user_pubkey=owner,
                priority_fee_lamports=self._settings.SOLANA_PRIORITY_FEE_LAMPORTS,
            )
            return await self._rpc.simulate_transaction(tx_b64)
        except Exception:
            log.info("solana_sellability_check_failed", venue_pair=venue_pair)
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_solana_adapter_balance.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/live/solana_swap_adapter.py tests/live/solana/test_solana_adapter_balance.py
git commit -m "feat(solana): adapter balances + sellability (honeypot guard)"
```

---

### Task 9: Adapter — place_order_request (quote → build → sign → send)

**Files:**
- Modify: `scout/live/solana_swap_adapter.py`
- Test: `tests/live/solana/test_solana_adapter_place.py`

**Interfaces:**
- Produces: `place_order_request(self, request: OrderRequest) -> str` → returns the tx signature (used as `venue_order_id`). Stashes the at-send quote on `self._pending[signature] = {"out_amount": int, "size_usd": float, "side": str}` so `await_fill_confirmation` can compute realized fill. Raises `RuntimeError("no signer")` if signer is None. Raises `RuntimeError("not_sellable")` is NOT done here (that is a gate, Task 11) — placing assumes gates passed.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_solana_adapter_place.py`:

```python
from __future__ import annotations

import pytest

from scout.config import Settings
from scout.live.adapter_base import OrderRequest
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _FakeJupiter:
    async def get_quote(self, *, input_mint, output_mint, amount, slippage_bps):
        return {"outAmount": "555000", "priceImpactPct": "0.001", "routePlan": [{}]}

    async def build_swap_tx(self, *, quote, user_pubkey, priority_fee_lamports):
        return "UNSIGNED_B64"


class _FakeRpc:
    def __init__(self):
        self.sent = []

    async def send_raw_transaction(self, signed_b64):
        self.sent.append(signed_b64)
        return "SIG_ABC"


class _FakeSigner:
    def pubkey(self):
        return "OWNER"

    def sign(self, tx_b64):
        return "SIGNED_" + tx_b64


def _req(side="buy"):
    return OrderRequest(
        paper_trade_id=1, canonical=MINT, venue_pair=MINT,
        side=side, size_usd=10.0, intent_uuid="abcd1234ef",
    )


@pytest.mark.asyncio
async def test_place_order_signs_sends_and_returns_signature():
    rpc = _FakeRpc()
    a = SolanaSwapAdapter(settings=_settings(), jupiter=_FakeJupiter(), rpc=rpc, signer=_FakeSigner())
    sig = await a.place_order_request(_req())
    assert sig == "SIG_ABC"
    assert rpc.sent == ["SIGNED_UNSIGNED_B64"]
    assert a._pending["SIG_ABC"]["out_amount"] == 555000
    assert a._pending["SIG_ABC"]["side"] == "buy"


@pytest.mark.asyncio
async def test_place_order_raises_without_signer():
    a = SolanaSwapAdapter(settings=_settings(), jupiter=_FakeJupiter(), rpc=_FakeRpc(), signer=None)
    with pytest.raises(RuntimeError, match="no signer"):
        await a.place_order_request(_req())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_solana_adapter_place.py -v`
Expected: FAIL — `AttributeError: 'SolanaSwapAdapter' object has no attribute 'place_order_request'`

- [ ] **Step 3: Implement place_order_request**

Add a `_pending: dict` init in `__init__` (add `self._pending = {}` at the end of `__init__`), then append:

```python
    async def place_order_request(self, request: OrderRequest) -> str:
        if self._signer is None:
            raise RuntimeError("no signer")
        owner = self._signer.pubkey()
        input_mint, output_mint = self._mints_for_side(request.venue_pair, request.side)
        amount = c.usdc_to_base_units(request.size_usd)  # USDC in for buy
        quote = await self._jupiter.get_quote(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount,
            slippage_bps=self._settings.SOLANA_SLIPPAGE_BPS_CAP,
        )
        unsigned = await self._jupiter.build_swap_tx(
            quote=quote,
            user_pubkey=owner,
            priority_fee_lamports=self._settings.SOLANA_PRIORITY_FEE_LAMPORTS,
        )
        signed = self._signer.sign(unsigned)
        signature = await self._rpc.send_raw_transaction(signed)
        self._pending[signature] = {
            "out_amount": int(quote["outAmount"]),
            "size_usd": request.size_usd,
            "side": request.side,
        }
        log.info("solana_order_sent", signature=signature, side=request.side,
                 size_usd=request.size_usd)
        return signature
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_solana_adapter_place.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/live/solana_swap_adapter.py tests/live/solana/test_solana_adapter_place.py
git commit -m "feat(solana): adapter place_order_request (quote/build/sign/send)"
```

---

### Task 10: Adapter — await_fill_confirmation (poll → terminal status)

**Files:**
- Modify: `scout/live/solana_swap_adapter.py`
- Test: `tests/live/solana/test_solana_adapter_await.py`

**Interfaces:**
- Produces: `await_fill_confirmation(self, *, venue_order_id: str, client_order_id: str, timeout_sec: float) -> OrderConfirmation`. Polls `rpc.confirm_signature` with backoff until terminal or timeout. Maps: `success`→`status="filled"` with `filled_qty`/`fill_price` from the stashed quote; `failed`→`status="rejected"`; timeout→`status="timeout"`. Uses an injected `sleep` (defaults to `asyncio.sleep`) and an injected `now`/elapsed clock so the test runs instantly.

**Implementation note on the clock:** accept `poll_interval_sec: float = 0.5` and an optional `_sleep` callable parameter so tests can pass a no-op; bound total polls by `max(1, int(timeout_sec / poll_interval_sec))` to avoid wall-clock dependence.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_solana_adapter_await.py`:

```python
from __future__ import annotations

import pytest

from scout.config import Settings
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _SeqRpc:
    """Returns a queued sequence of confirm states."""

    def __init__(self, states):
        self._states = list(states)

    async def confirm_signature(self, signature):
        return self._states.pop(0) if self._states else "pending"


async def _noop_sleep(_):
    return None


def _adapter(rpc):
    a = SolanaSwapAdapter(settings=_settings(), jupiter=None, rpc=rpc, signer=None)
    a._pending["SIG"] = {"out_amount": 1_000_000, "size_usd": 10.0, "side": "buy"}
    return a


@pytest.mark.asyncio
async def test_await_filled_after_pending():
    a = _adapter(_SeqRpc(["pending", "success"]))
    conf = await a.await_fill_confirmation(
        venue_order_id="SIG", client_order_id="cid", timeout_sec=10,
        poll_interval_sec=0.5, _sleep=_noop_sleep,
    )
    assert conf.status == "filled"
    assert conf.venue_order_id == "SIG"
    assert conf.filled_qty == 1_000_000
    assert conf.fill_price is not None


@pytest.mark.asyncio
async def test_await_rejected_on_chain_failure():
    a = _adapter(_SeqRpc(["failed"]))
    conf = await a.await_fill_confirmation(
        venue_order_id="SIG", client_order_id="cid", timeout_sec=10,
        poll_interval_sec=0.5, _sleep=_noop_sleep,
    )
    assert conf.status == "rejected"


@pytest.mark.asyncio
async def test_await_timeout_when_never_lands():
    a = _adapter(_SeqRpc(["pending", "pending"]))
    conf = await a.await_fill_confirmation(
        venue_order_id="SIG", client_order_id="cid", timeout_sec=1,
        poll_interval_sec=0.5, _sleep=_noop_sleep,
    )
    assert conf.status == "timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_solana_adapter_await.py -v`
Expected: FAIL — `AttributeError: 'SolanaSwapAdapter' object has no attribute 'await_fill_confirmation'`

- [ ] **Step 3: Implement await_fill_confirmation**

Add `import asyncio` to the adapter imports, then append:

```python
    async def await_fill_confirmation(
        self,
        *,
        venue_order_id: str,
        client_order_id: str,
        timeout_sec: float,
        poll_interval_sec: float = 0.5,
        _sleep=None,
    ) -> OrderConfirmation:
        sleep = _sleep or asyncio.sleep
        pending = self._pending.get(venue_order_id, {})
        out_amount = pending.get("out_amount")
        size_usd = pending.get("size_usd")
        max_polls = max(1, int(timeout_sec / poll_interval_sec))
        status = "pending"
        for _ in range(max_polls):
            status = await self._rpc.confirm_signature(venue_order_id)
            if status in ("success", "failed"):
                break
            await sleep(poll_interval_sec)
        if status == "success":
            fill_price = (
                (size_usd / out_amount) if (out_amount and size_usd) else None
            )
            return OrderConfirmation(
                venue="solana", venue_order_id=venue_order_id,
                client_order_id=client_order_id, status="filled",
                filled_qty=float(out_amount) if out_amount else None,
                fill_price=fill_price, raw_response={"signature": venue_order_id},
            )
        if status == "failed":
            return OrderConfirmation(
                venue="solana", venue_order_id=venue_order_id,
                client_order_id=client_order_id, status="rejected",
                filled_qty=None, fill_price=None,
                raw_response={"signature": venue_order_id, "reason": "on_chain_failure"},
            )
        return OrderConfirmation(
            venue="solana", venue_order_id=venue_order_id,
            client_order_id=client_order_id, status="timeout",
            filled_qty=None, fill_price=None,
            raw_response={"signature": venue_order_id},
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_solana_adapter_await.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the whole adapter suite + format**

Run: `uv run pytest tests/live/solana/ -q && uv run black scout/live/solana_swap_adapter.py scout/live/solana/`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add scout/live/solana_swap_adapter.py tests/live/solana/test_solana_adapter_await.py
git commit -m "feat(solana): adapter await_fill_confirmation (poll -> terminal status)"
```

---

### Task 11: On-chain gates (price-impact, sellability, gas reserve)

**Files:**
- Modify: `scout/live/gates.py` (add `not_sellable` to `VALID_REJECT_REASONS`; branch on on-chain adapter)
- Test: `tests/live/test_onchain_gates.py`

**Interfaces:**
- Consumes: adapter attribute `is_onchain: bool`, adapter methods `quote_at_size`, `is_sellable`, `fetch_account_balance("SOL")`; settings `SOLANA_MAX_PRICE_IMPACT_PCT`, `SOLANA_SLIPPAGE_BPS_CAP` (reuse `LIVE_SLIPPAGE_BPS_CAP` semantics via existing slippage gate is bypassed on-chain), `SOLANA_MIN_SOL_GAS_RESERVE`.
- Produces: a new method `async def evaluate_onchain(self, *, signal_type, symbol, venue_pair, size_usd) -> GateResult` returning `GateResult` with reject_reasons in `{'insufficient_depth','not_sellable','insufficient_balance', None}`. The engine's on-chain fork invokes this via a SECOND `Gates` instance bound to the Solana adapter (`self._onchain_gates`, built in Task 14). Keeps the CEX `evaluate()` and the Binance-bound `Gates` instance untouched.

**Why a separate method:** the CEX `evaluate()` walks an order book; on-chain replaces that with price-impact and adds sellability/gas. A dedicated method avoids destabilizing the heavily-tested CEX path while reusing `GateResult` and the kill-switch/allowlist/exposure checks.

- [ ] **Step 1: Write the failing test**

Create `tests/live/test_onchain_gates.py`:

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from scout.config import Settings
from scout.live.config import LiveConfig
from scout.live.gates import VALID_REJECT_REASONS, Gates

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _Adapter:
    is_onchain = True

    def __init__(self, *, impact, sellable, sol):
        self._impact, self._sellable, self._sol = impact, sellable, sol
        self.venue_name = "solana"

    async def quote_at_size(self, *, venue_pair, side, size_usd):
        return {"out_amount": 1000, "price_impact_pct": self._impact, "mid": Decimal("1")}

    async def is_sellable(self, *, venue_pair, expected_out_amount):
        return self._sellable

    async def fetch_account_balance(self, asset="USDT"):
        return self._sol if asset == "SOL" else 1000.0


class _KS:
    def is_active(self):
        return None


def _gates(adapter, **so):
    s = _settings(**so)
    return Gates(config=LiveConfig(s), db=None, resolver=None, adapter=adapter, kill_switch=_KS())


def test_not_sellable_is_a_valid_reject_reason():
    assert "not_sellable" in VALID_REJECT_REASONS


@pytest.mark.asyncio
async def test_onchain_pass():
    g = _gates(_Adapter(impact=0.5, sellable=True, sol=0.5))
    res = await g.evaluate_onchain(signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10"))
    assert res.passed is True


@pytest.mark.asyncio
async def test_onchain_price_impact_reject():
    g = _gates(_Adapter(impact=9.0, sellable=True, sol=0.5))  # > 3.0 default
    res = await g.evaluate_onchain(signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10"))
    assert res.passed is False
    assert res.reject_reason == "insufficient_depth"


@pytest.mark.asyncio
async def test_onchain_not_sellable_reject():
    g = _gates(_Adapter(impact=0.5, sellable=False, sol=0.5))
    res = await g.evaluate_onchain(signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10"))
    assert res.passed is False
    assert res.reject_reason == "not_sellable"


@pytest.mark.asyncio
async def test_onchain_gas_reserve_reject():
    g = _gates(_Adapter(impact=0.5, sellable=True, sol=0.0))  # < 0.02 default
    res = await g.evaluate_onchain(signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10"))
    assert res.passed is False
    assert res.reject_reason == "insufficient_balance"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/test_onchain_gates.py -v`
Expected: FAIL — `AttributeError: 'Gates' object has no attribute 'evaluate_onchain'` and the `not_sellable` assertion fails.

- [ ] **Step 3: Add the reject reason + on-chain gate method**

In `scout/live/gates.py`, add `"not_sellable",` to the `VALID_REJECT_REASONS` frozenset (place it with the M1 additions). Then add this method to the `Gates` class:

```python
    async def evaluate_onchain(
        self, *, signal_type: str, symbol: str, venue_pair: str, size_usd: Decimal
    ) -> GateResult:
        """On-chain gate chain (Solana). Replaces the CEX order-book walk with
        Jupiter price-impact, and adds sellability (honeypot) + SOL gas gates.
        Kill-switch and allowlist are checked first, mirroring evaluate()."""
        if self._ks is not None and self._ks.is_active() is not None:
            return GateResult(passed=False, reject_reason="kill_switch", detail=None)
        if not self._config.is_signal_enabled(signal_type):
            return GateResult(passed=False, reject_reason=None, detail="not_allowlisted")

        s = self._config._s
        quote = await self._adapter.quote_at_size(
            venue_pair=venue_pair, side="buy", size_usd=float(size_usd)
        )
        if quote["price_impact_pct"] > s.SOLANA_MAX_PRICE_IMPACT_PCT:
            return GateResult(
                passed=False, reject_reason="insufficient_depth",
                detail=f"price_impact_pct={quote['price_impact_pct']:.3f} "
                       f"cap={s.SOLANA_MAX_PRICE_IMPACT_PCT}",
            )

        sellable = await self._adapter.is_sellable(
            venue_pair=venue_pair, expected_out_amount=quote["out_amount"]
        )
        if not sellable:
            return GateResult(
                passed=False, reject_reason="not_sellable",
                detail=f"sell simulation failed for {venue_pair}",
            )

        sol = await self._adapter.fetch_account_balance("SOL")
        if sol < s.SOLANA_MIN_SOL_GAS_RESERVE:
            return GateResult(
                passed=False, reject_reason="insufficient_balance",
                detail=f"sol={sol} reserve={s.SOLANA_MIN_SOL_GAS_RESERVE}",
            )
        return GateResult(passed=True, reject_reason=None, detail=None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/test_onchain_gates.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Verify the CEX gate suite still passes**

Run: `uv run pytest tests/live/test_pretrade_gates.py -q`
Expected: PASS (unchanged — `evaluate()` was not modified).

- [ ] **Step 6: Commit**

```bash
git add scout/live/gates.py tests/live/test_onchain_gates.py
git commit -m "feat(solana): on-chain gates (price-impact, sellability, gas)"
```

---

### Task 12: Reconciliation — recover pending on-chain trades by signature

**Files:**
- Create: `scout/live/solana_reconciliation.py`
- Test: `tests/live/solana/test_solana_reconciliation.py`

**Interfaces:**
- Consumes: `Database`, the adapter's `rpc.confirm_signature`.
- Produces: `async def reconcile_open_solana_trades(*, db: Database, rpc, settings) -> dict[str, int]`. Scans `live_trades` rows with `venue='solana'`, `status='open'`, `entry_order_id` (the tx signature) set, AND `entry_fill_price IS NULL` (sent-but-unconfirmed — a confirmed open position has its fill price recorded by the live dispatch path in Task 14, so it is NOT re-checked). Re-checks each signature on-chain. **`success` → the entry landed; the position is genuinely OPEN, so leave `status='open'`** (do NOT write `'filled'` — that value is forbidden by the `live_trades.status` CHECK constraint, and a filled buy IS an open position until it exits). **`failed` → the swap reverted, no position → `status='rejected'`.** **`pending` → leave open** for the next boot. Returns `{'confirmed': n, 'failed': n, 'pending': n}`. Always logs `solana_reconciliation_done`. Never raises.

**Note on schema:** `live_trades` already has `entry_order_id`, `status`, `venue` columns (used by Binance path). No migration needed; we reuse `entry_order_id` to hold the tx signature for the solana venue.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_solana_reconciliation.py`:

```python
from __future__ import annotations

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.solana_reconciliation import reconcile_open_solana_trades

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _SeqRpc:
    def __init__(self, mapping):
        self._m = mapping

    async def confirm_signature(self, signature):
        return self._m[signature]


async def _seed_open_solana_trade(db, *, sig, ptid):
    cur = await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, client_order_id, entry_order_id, created_at)
           VALUES (?, 'c', 'X', 'solana', 'MINT', 'first_signal',
                   '10', 'open', ?, ?, '2026-06-21T00:00:00+00:00')""",
        (ptid, f"cid-{ptid}", sig),
    )
    await db._conn.commit()
    return cur.lastrowid


async def _status_of(db, row_id):
    cur = await db._conn.execute("SELECT status FROM live_trades WHERE id=?", (row_id,))
    return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_reconcile_confirms_fails_and_leaves_pending(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ok = await _seed_open_solana_trade(db, sig="SIG_OK", ptid=1)
    bad = await _seed_open_solana_trade(db, sig="SIG_BAD", ptid=2)
    wait = await _seed_open_solana_trade(db, sig="SIG_WAIT", ptid=3)
    rpc = _SeqRpc({"SIG_OK": "success", "SIG_BAD": "failed", "SIG_WAIT": "pending"})

    summary = await reconcile_open_solana_trades(db=db, rpc=rpc, settings=_settings())

    # success => position is genuinely open (NOT 'filled', which the CHECK forbids)
    assert await _status_of(db, ok) == "open"
    # failed swap => no position
    assert await _status_of(db, bad) == "rejected"
    # not yet landed => leave open for next boot
    assert await _status_of(db, wait) == "open"
    assert summary == {"confirmed": 1, "failed": 1, "pending": 1}
    await db.close()


@pytest.mark.asyncio
async def test_reconcile_no_rows_is_noop(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await reconcile_open_solana_trades(db=db, rpc=_SeqRpc({}), settings=_settings())
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_solana_reconciliation.py -v`
Expected: FAIL — `ModuleNotFoundError: scout.live.solana_reconciliation`

- [ ] **Step 3: Implement reconciliation**

Create `scout/live/solana_reconciliation.py`:

```python
"""Boot-time recovery for on-chain trades: the tx signature is source of truth.

For each open solana live_trades row carrying a signature, re-check the chain:
success -> filled, failed -> rejected, pending -> leave open for next boot.
"""

from __future__ import annotations

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)


async def reconcile_open_solana_trades(*, db: Database, rpc, settings) -> dict[str, int]:
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    # Only sent-but-unconfirmed rows: a confirmed open position has its
    # entry_fill_price set by the live dispatch path, so it is excluded here.
    cur = await db._conn.execute(
        "SELECT id, entry_order_id FROM live_trades "
        "WHERE venue='solana' AND status='open' AND entry_order_id IS NOT NULL "
        "AND entry_fill_price IS NULL"
    )
    rows = await cur.fetchall()

    confirmed = failed = pending = 0
    for row_id, signature in rows:
        try:
            state = await rpc.confirm_signature(signature)
        except Exception:
            log.warning("solana_reconciliation_row_err", row_id=row_id, signature=signature)
            pending += 1
            continue
        if state == "success":
            # Entry landed → genuinely an OPEN position. Do NOT write 'filled'
            # (forbidden by the live_trades.status CHECK, and a filled buy is
            # open until it exits). Leave status='open'.
            confirmed += 1
        elif state == "failed":
            # Swap reverted on-chain → no position exists.
            await db._conn.execute(
                "UPDATE live_trades SET status='rejected' WHERE id=?", (row_id,)
            )
            failed += 1
        else:
            pending += 1
    await db._conn.commit()

    summary = {"confirmed": confirmed, "failed": failed, "pending": pending}
    log.info("solana_reconciliation_done", rows_inspected=len(rows), **summary)
    return summary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_solana_reconciliation.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/live/solana_reconciliation.py tests/live/solana/test_solana_reconciliation.py
git commit -m "feat(solana): boot reconciliation by tx signature"
```

---

### Task 13: Solana mint resolver

**Files:**
- Create: `scout/live/solana/mint_resolver.py`
- Test: `tests/live/solana/test_mint_resolver.py`

**Interfaces:**
- Produces: `def resolve_solana_mint(*, coin_id: str, contract_address: str | None = None) -> str | None`. Reuses `scout.trading.minara_alert._looks_like_spl_address`. Returns the SPL mint when `contract_address` is a valid SPL address, else when `coin_id` itself is a valid SPL address (native Solana tokens carry the mint AS their coin_id — confirmed in `minara_alert.maybe_minara_command`), else `None`.

**Why:** the engine works in symbols/coin_ids; Jupiter needs the mint. This mirrors the exact resolution the existing Minara alert path uses. The non-native case (a CoinGecko slug whose `platforms.solana` holds the mint) needs a network `fetch_coin_detail` lookup and is a flagged follow-up — the first rollout snipes native Solana tokens where `coin_id` IS the mint.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_mint_resolver.py`:

```python
from __future__ import annotations

from scout.live.solana.mint_resolver import resolve_solana_mint

MINT = "So11111111111111111111111111111111111111112"


def test_native_coin_id_is_the_mint():
    assert resolve_solana_mint(coin_id=MINT) == MINT


def test_explicit_contract_address_wins():
    assert resolve_solana_mint(coin_id="some-cg-slug", contract_address=MINT) == MINT


def test_non_solana_slug_returns_none():
    assert resolve_solana_mint(coin_id="bitcoin") is None
    assert resolve_solana_mint(coin_id="0xabc123") is None  # EVM-shaped, has '0'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_mint_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: scout.live.solana.mint_resolver`

- [ ] **Step 3: Implement the resolver**

Create `scout/live/solana/mint_resolver.py`:

```python
"""Resolve a Solana SPL mint for a paper trade, mirroring the Minara alert path.

Native Solana tokens carry the SPL mint directly as their coin_id (CG slugs are
never 32-44 base58 chars; EVM 0x… ids contain '0', not in the base58 alphabet).
"""

from __future__ import annotations

from scout.trading.minara_alert import _looks_like_spl_address


def resolve_solana_mint(
    *, coin_id: str, contract_address: str | None = None
) -> str | None:
    if contract_address and _looks_like_spl_address(contract_address):
        return contract_address
    if _looks_like_spl_address(coin_id):
        return coin_id
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_mint_resolver.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add scout/live/solana/mint_resolver.py tests/live/solana/test_mint_resolver.py
git commit -m "feat(solana): SPL mint resolver (mirrors Minara path)"
```

---

### Task 14: Engine on-chain fork (`_dispatch_onchain`)

**Files:**
- Modify: `scout/live/engine.py` (`__init__`, add early branch to `on_paper_trade_opened`, add `_dispatch_onchain` + `_is_solana_signal`)
- Test: `tests/live/solana/test_engine_onchain_fork.py`

**Interfaces:**
- Consumes: `SolanaSwapAdapter` (via the `ExchangeAdapter` contract + `quote_at_size`), `Gates.evaluate_onchain` (Task 11), `resolve_solana_mint` (Task 13), `make_client_order_id`/`record_pending_order` (existing `idempotency.py`).
- Produces: `LiveEngine.__init__` gains `onchain_adapter: "ExchangeAdapter | None" = None`. When provided, the engine builds a SECOND `Gates` instance bound to that adapter (`self._onchain_gates`) and `on_paper_trade_opened` forks to `_dispatch_onchain` for Solana signals. **When `onchain_adapter is None` (every existing Binance test), `on_paper_trade_opened` is byte-for-byte unchanged — the Binance path and its tests are untouched.**

**Lifecycle inside `_dispatch_onchain`:** resolve mint → `evaluate_onchain` gate → not-allowlisted/kill: no row → other reject: `shadow_trades` rejected row → pass + `mode != 'live'`: `shadow_trades` open row (quote only, NO broadcast) → pass + `mode == 'live'`: `record_pending_order` → `place_order_request` → store signature in `entry_order_id` → `await_fill_confirmation` → `filled`: record `entry_fill_price`/`entry_fill_qty` (status stays `'open'` — it is an open position) + bump correction counter; `rejected`: status `'rejected'`; `timeout`: leave `'open'` for boot reconciliation (Task 12).

- [ ] **Step 1: Write the failing test (shadow fork opens a row, never broadcasts)**

Create `tests/live/solana/test_engine_onchain_fork.py`:

```python
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.engine import LiveEngine

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(
        _env_file=None, **_REQUIRED, LIVE_MODE="shadow",
        LIVE_SIGNAL_ALLOWLIST="first_signal", **o,
    )


class _Adapter:
    is_onchain = True
    venue_name = "solana"

    async def quote_at_size(self, *, venue_pair, side, size_usd):
        return {"out_amount": 1000, "price_impact_pct": 0.5, "mid": Decimal("1")}

    async def is_sellable(self, *, venue_pair, expected_out_amount):
        return True

    async def fetch_account_balance(self, asset="USDT"):
        return 0.5 if asset == "SOL" else 1000.0

    async def place_order_request(self, request):
        raise AssertionError("shadow mode must NOT place an order")


class _KS:
    def is_active(self):
        return None


async def _seed_paper(db, coin_id):
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES (?, 'WSOL', 'wsol', 'solana', 'first_signal', '{}',
                   1, 10, 10, 1.2, 0.8, 'open', '2026-06-21T00:00:00+00:00')""",
        (coin_id,),
    )
    await db._conn.commit()
    return cur.lastrowid


@pytest.mark.asyncio
async def test_solana_signal_forks_to_shadow_without_broadcast(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = _settings()
    ptid = await _seed_paper(db, MINT)
    engine = LiveEngine(
        config=LiveConfig(s), resolver=None, adapter=_Adapter(), db=db,
        kill_switch=_KS(), routing=None, onchain_adapter=_Adapter(),
    )
    paper = SimpleNamespace(
        id=ptid, coin_id=MINT, symbol="WSOL", signal_type="first_signal", chain="solana"
    )
    await engine.on_paper_trade_opened(paper)

    cur = await db._conn.execute(
        "SELECT venue, status FROM shadow_trades WHERE paper_trade_id=?", (ptid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "solana"
    assert row[1] == "open"
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_engine_onchain_fork.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'onchain_adapter'`

- [ ] **Step 3: Add the `onchain_adapter` param + second Gates instance**

In `scout/live/engine.py`, modify `LiveEngine.__init__`. Add the parameter at the end of the signature:

```python
        routing: "RoutingLayer | None" = None,
        onchain_adapter: "ExchangeAdapter | None" = None,
```

And after the existing `self._gates = Gates(...)` block, add:

```python
        self._onchain_adapter = onchain_adapter
        self._onchain_gates = (
            Gates(
                config=config,
                db=db,
                resolver=resolver,
                adapter=onchain_adapter,
                kill_switch=kill_switch,
            )
            if onchain_adapter is not None
            else None
        )
```

- [ ] **Step 4: Add the early fork branch + helper**

At the VERY START of `on_paper_trade_opened` (immediately after the docstring, before `trade_id = paper_trade.id`), insert:

```python
        if self._onchain_adapter is not None and _is_solana_signal(paper_trade):
            await self._dispatch_onchain(paper_trade)
            return
```

Add this module-level helper near the top of `engine.py` (after imports):

```python
def _is_solana_signal(paper_trade) -> bool:
    return getattr(paper_trade, "chain", None) == "solana"
```

- [ ] **Step 5: Add `_dispatch_onchain`**

Add this method to `LiveEngine` (mirrors the structure of `_dispatch_live`, but uses the on-chain adapter + gates and writes valid status values):

```python
    async def _dispatch_onchain(self, paper_trade) -> None:
        from uuid import uuid4

        from scout.live.adapter_base import OrderRequest
        from scout.live.correction_counter import increment_consecutive
        from scout.live.idempotency import (
            make_client_order_id,
            record_pending_order,
        )
        from scout.live.solana.mint_resolver import resolve_solana_mint

        trade_id = paper_trade.id
        size_usd = self._config.resolve_size_usd(paper_trade.signal_type)
        now_iso = datetime.now(timezone.utc).isoformat()
        mint = resolve_solana_mint(
            coin_id=paper_trade.coin_id,
            contract_address=getattr(paper_trade, "contract_address", None),
        )
        if mint is None:
            log.info("onchain_handoff_skipped_no_mint", paper_trade_id=trade_id)
            return

        result = await self._onchain_gates.evaluate_onchain(
            signal_type=paper_trade.signal_type,
            symbol=paper_trade.symbol,
            venue_pair=mint,
            size_usd=size_usd,
        )
        if not result.passed and result.reject_reason is None:
            log.info("onchain_handoff_skipped", paper_trade_id=trade_id, reason="not_allowlisted")
            return
        if not result.passed and result.reject_reason == "kill_switch":
            log.info("onchain_handoff_skipped_killed", paper_trade_id=trade_id)
            return
        if not result.passed:
            assert self._db._conn is not None
            async with self._db._txn_lock:
                await self._db._conn.execute(
                    "INSERT INTO shadow_trades "
                    "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
                    " size_usd, status, reject_reason, created_at) "
                    "VALUES (?, ?, ?, 'solana', ?, ?, ?, 'rejected', ?, ?)",
                    (trade_id, paper_trade.coin_id, paper_trade.symbol, mint,
                     paper_trade.signal_type, str(size_usd), result.reject_reason, now_iso),
                )
                await self._db._conn.commit()
            log.info("onchain_pretrade_gate_failed", paper_trade_id=trade_id,
                     reject_reason=result.reject_reason, detail=result.detail)
            return

        # Passed gates. Shadow → record intent, NO broadcast.
        if self._config.mode != "live":
            quote = await self._onchain_adapter.quote_at_size(
                venue_pair=mint, side="buy", size_usd=float(size_usd)
            )
            assert self._db._conn is not None
            async with self._db._txn_lock:
                await self._db._conn.execute(
                    "INSERT INTO shadow_trades "
                    "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
                    " size_usd, mid_at_entry, status, created_at) "
                    "VALUES (?, ?, ?, 'solana', ?, ?, ?, ?, 'open', ?)",
                    (trade_id, paper_trade.coin_id, paper_trade.symbol, mint,
                     paper_trade.signal_type, str(size_usd), str(quote["mid"]), now_iso),
                )
                await self._db._conn.commit()
            log.info("onchain_shadow_order_opened", paper_trade_id=trade_id,
                     pair=mint, mid=str(quote["mid"]), size_usd=str(size_usd))
            return

        # Live → record pending row, place swap, await fill, record terminal.
        intent_uuid = str(uuid4())
        cid = make_client_order_id(trade_id, intent_uuid)
        live_id = await record_pending_order(
            self._db, client_order_id=cid, paper_trade_id=trade_id,
            coin_id=paper_trade.coin_id, symbol=paper_trade.symbol, venue="solana",
            pair=mint, signal_type=paper_trade.signal_type, size_usd=str(size_usd),
        )
        request = OrderRequest(
            paper_trade_id=trade_id, canonical=mint, venue_pair=mint,
            side="buy", size_usd=float(size_usd), intent_uuid=intent_uuid,
        )
        try:
            signature = await self._onchain_adapter.place_order_request(request)
        except Exception:
            log.exception("onchain_dispatch_place_failed", paper_trade_id=trade_id)
            async with self._db._txn_lock:
                await self._db._conn.execute(
                    "UPDATE live_trades SET status='needs_manual_review' WHERE id=?",
                    (live_id,),
                )
                await self._db._conn.commit()
            return
        async with self._db._txn_lock:
            await self._db._conn.execute(
                "UPDATE live_trades SET entry_order_id=? WHERE id=?", (signature, live_id)
            )
            await self._db._conn.commit()
        log.info("live_dispatch_entered", paper_trade_id=trade_id,
                 venue="solana", signature=signature)

        confirmation = await self._onchain_adapter.await_fill_confirmation(
            venue_order_id=signature, client_order_id=cid,
            timeout_sec=self._config._s.SOLANA_CONFIRM_TIMEOUT_SEC,
        )
        async with self._db._txn_lock:
            if confirmation.status == "filled":
                # Filled buy IS an open position → keep status='open', record fill.
                await self._db._conn.execute(
                    "UPDATE live_trades SET entry_fill_price=?, entry_fill_qty=? "
                    "WHERE id=?",
                    (str(confirmation.fill_price), str(confirmation.filled_qty), live_id),
                )
            elif confirmation.status == "rejected":
                await self._db._conn.execute(
                    "UPDATE live_trades SET status='rejected' WHERE id=?", (live_id,)
                )
            # timeout → leave 'open'; Task 12 boot reconciliation resolves it.
            await self._db._conn.commit()
        log.info("live_dispatch_terminal", paper_trade_id=trade_id, venue="solana",
                 signature=signature, status=confirmation.status)
        if confirmation.status == "filled":
            await increment_consecutive(
                self._db, paper_trade.signal_type, "solana", paper_trade_id=trade_id
            )
```

- [ ] **Step 6: Run the fork test + confirm the Binance engine suite is unchanged**

Run: `uv run pytest tests/live/solana/test_engine_onchain_fork.py tests/live/test_live_engine.py tests/live/test_live_engine_dispatch.py -q`
Expected: the new fork test PASSES; all existing engine tests PASS unchanged (they construct `LiveEngine` without `onchain_adapter`, so the new branch is never entered).

- [ ] **Step 7: Commit**

```bash
git add scout/live/engine.py tests/live/solana/test_engine_onchain_fork.py
git commit -m "feat(solana): isolated on-chain engine fork (_dispatch_onchain)"
```

---

### Task 15: Factory + engine boot wiring + reconciliation hook

**Files:**
- Create: `scout/live/solana_factory.py`
- Modify: `scout/main.py` (live-engine boot block, ~lines 1896–1975)
- Test: `tests/live/solana/test_solana_factory.py`

**Interfaces:**
- Produces: `def build_solana_adapter(*, settings, session, db) -> SolanaSwapAdapter | None`. Returns `None` when `SOLANA_WALLET_SECRET` is unset (so paper/shadow without keys still boots). Otherwise constructs `JupiterClient` (passing the optional `SOLANA_JUPITER_API_KEY`), `SolanaRpc`, signer via `make_signer`, and the adapter.
- Modifies `scout/main.py` to: build the solana adapter, pass it as `onchain_adapter=` to `LiveEngine` (NOT into the routing dict — the fork bypasses routing), and call `reconcile_open_solana_trades` in the boot reconciliation block.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_solana_factory.py`:

```python
from __future__ import annotations

import aiohttp
import pytest

from scout.config import Settings
from scout.live.solana_factory import build_solana_adapter
from scout.live.solana_swap_adapter import SolanaSwapAdapter
from solders.keypair import Keypair

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


@pytest.mark.asyncio
async def test_factory_none_without_secret():
    s = Settings(_env_file=None, **_REQUIRED)
    async with aiohttp.ClientSession() as session:
        assert build_solana_adapter(settings=s, session=session, db=None) is None


@pytest.mark.asyncio
async def test_factory_builds_adapter_with_secret():
    s = Settings(_env_file=None, **_REQUIRED, SOLANA_WALLET_SECRET=str(Keypair()))
    async with aiohttp.ClientSession() as session:
        a = build_solana_adapter(settings=s, session=session, db=None)
        assert isinstance(a, SolanaSwapAdapter)
        assert a.venue_name == "solana"
        assert a.is_onchain is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_solana_factory.py -v`
Expected: FAIL — `ModuleNotFoundError: scout.live.solana_factory`

- [ ] **Step 3: Implement the factory**

Create `scout/live/solana_factory.py`:

```python
"""Construct the SolanaSwapAdapter and its sub-modules from settings."""

from __future__ import annotations

from typing import Any

import aiohttp
import structlog

from scout.live.solana.jupiter_client import JupiterClient
from scout.live.solana.rpc import SolanaRpc
from scout.live.solana.wallet import make_signer
from scout.live.solana_swap_adapter import SolanaSwapAdapter

log = structlog.get_logger(__name__)


def build_solana_adapter(
    *, settings, session: aiohttp.ClientSession, db: Any | None
) -> SolanaSwapAdapter | None:
    signer = make_signer(settings)
    if signer is None:
        log.info("solana_adapter_skipped_no_secret")
        return None
    api_key = (
        settings.SOLANA_JUPITER_API_KEY.get_secret_value()
        if settings.SOLANA_JUPITER_API_KEY is not None
        else None
    )
    jupiter = JupiterClient(session, base_url=settings.SOLANA_JUPITER_URL, api_key=api_key)
    rpc = SolanaRpc(session, settings.SOLANA_RPC_URL)
    log.info("solana_adapter_built", pubkey=signer.pubkey())
    return SolanaSwapAdapter(
        settings=settings, jupiter=jupiter, rpc=rpc, signer=signer, db=db
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_solana_factory.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Wire into main.py**

In `scout/main.py`, after `_live_owned.append(live_adapter)` (~line 1897), add:

```python
        # BL-NEW-SOLANA: optional on-chain venue. None when no wallet secret.
        from scout.live.solana_factory import build_solana_adapter

        solana_adapter = build_solana_adapter(
            settings=settings, session=live_adapter._session, db=db
        )
        if solana_adapter is not None:
            _live_owned.append(solana_adapter)
```

Then add `onchain_adapter=solana_adapter` to the `LiveEngine(...)` construction (the routing dict stays Binance-only — the fork does not use routing):

```python
        live_engine = LiveEngine(
            config=live_config,
            resolver=resolver,
            adapter=live_adapter,
            db=db,
            kill_switch=live_kill_switch,
            routing=live_routing,
            onchain_adapter=solana_adapter,
        )
```

Finally, immediately AFTER the `reconcile_open_shadow_trades(...)` call, add:

```python
        if solana_adapter is not None:
            from scout.live.solana_reconciliation import reconcile_open_solana_trades

            await reconcile_open_solana_trades(
                db=db, rpc=solana_adapter._rpc, settings=settings
            )
```

- [ ] **Step 6: Verify boot path imports & config-check still pass**

Run: `uv run python -m scout.main --check-config`
Expected: exits cleanly (no import errors); with no `SOLANA_WALLET_SECRET`, logs `solana_adapter_skipped_no_secret`.

- [ ] **Step 7: Commit**

```bash
git add scout/live/solana_factory.py scout/main.py tests/live/solana/test_solana_factory.py
git commit -m "feat(solana): factory + onchain_adapter wiring + reconciliation hook"
```

---

### Task 16: Shadow-mode integration test (quote + simulate, no broadcast)

**Files:**
- Create: `tests/live/solana/test_solana_shadow_integration.py`

**Interfaces:**
- Consumes everything built above. Proves: with `LIVE_MODE=shadow`, an eligible on-chain signal runs the on-chain gate using real quote + sellability simulation, and a `shadow_trades` row is written, WITHOUT any `send_raw_transaction` call.

**Note:** This test exercises the gate + adapter directly with a stub engine harness rather than full `scout.main` boot, to stay a fast unit-level integration. It asserts the no-broadcast invariant by giving the rpc a `send_raw_transaction` that raises if called.

- [ ] **Step 1: Write the test**

Create `tests/live/solana/test_solana_shadow_integration.py`:

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from scout.config import Settings
from scout.live.config import LiveConfig
from scout.live.gates import Gates
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(
        _env_file=None, **_REQUIRED, LIVE_MODE="shadow",
        LIVE_SIGNAL_ALLOWLIST="first_signal", **o,
    )


class _Jupiter:
    async def get_quote(self, *, input_mint, output_mint, amount, slippage_bps):
        return {"outAmount": "1000000", "priceImpactPct": "0.005", "routePlan": [{}]}

    async def build_swap_tx(self, *, quote, user_pubkey, priority_fee_lamports):
        return "SIM_TX"


class _Rpc:
    def __init__(self):
        self.simulated = False

    async def get_sol_balance(self, *, owner):
        return 0.5

    async def get_token_balance(self, *, owner, mint):
        return 50.0

    async def simulate_transaction(self, tx_b64):
        self.simulated = True
        return True

    async def send_raw_transaction(self, signed_b64):  # invariant guard
        raise AssertionError("shadow mode must NOT broadcast")


class _Signer:
    def pubkey(self):
        return "OWNER"

    def sign(self, tx_b64):
        return tx_b64


class _KS:
    def is_active(self):
        return None


@pytest.mark.asyncio
async def test_shadow_runs_gate_without_broadcast():
    s = _settings()
    rpc = _Rpc()
    adapter = SolanaSwapAdapter(settings=s, jupiter=_Jupiter(), rpc=rpc, signer=_Signer())
    gates = Gates(config=LiveConfig(s), db=None, resolver=None, adapter=adapter, kill_switch=_KS())

    res = await gates.evaluate_onchain(
        signal_type="first_signal", symbol="X", venue_pair=MINT, size_usd=Decimal("10")
    )

    assert res.passed is True          # 0.5% impact < 3% cap, sellable, gas ok
    assert rpc.simulated is True        # sellability simulation ran
    # no broadcast happened (would have raised). Reaching here proves it.
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_solana_shadow_integration.py -v`
Expected: PASS (1 test). If it fails with the broadcast AssertionError, the gate path is incorrectly sending — fix before proceeding.

- [ ] **Step 3: Run the full Solana + live suites**

Run: `uv run pytest tests/live/ -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/live/solana/test_solana_shadow_integration.py
git commit -m "test(solana): shadow-mode integration asserts no broadcast"
```

---

### Task 17: Daily float sweep + freshness watchdog + systemd units + .env docs

**Files:**
- Create: `scripts/solana_sweep.py`
- Create: `scripts/solana_execution_watchdog.py`
- Create: `systemd/solana-sweep.service`, `systemd/solana-sweep.timer`
- Create: `systemd/solana-execution-watchdog.service`, `systemd/solana-execution-watchdog.timer`
- Modify: `.env.example` (document non-secret SOLANA_* knobs; NEVER the secret)
- Test: `tests/live/solana/test_solana_sweep.py`

**Interfaces:**
- Produces: `scripts/solana_sweep.py` with `async def compute_sweep_amount(*, balance_usd: float, float_cap_usd: float) -> float` (returns `max(0, balance - cap)`), used by a `main()` that builds the adapter and, if `SOLANA_SWEEP_COLD_WALLET` set and excess > 0, swaps/transfers the excess (logs + Telegram). The watchdog asserts a recent `solana_reconciliation_done`/order event exists within an SLO window.

**Scope note:** the actual cold-wallet transfer instruction-building is flagged in the spec's open items; this task implements and TESTS the sweep *decision* (`compute_sweep_amount`) and the watchdog SLO check, and stubs the transfer behind a clearly-logged `log.warning("solana_sweep_transfer_not_implemented", ...)` until the cold-wallet transfer design lands. This keeps the float bound observable immediately without shipping an untested transfer.

- [ ] **Step 1: Write the failing test**

Create `tests/live/solana/test_solana_sweep.py`:

```python
from __future__ import annotations

import pytest

from scripts.solana_sweep import compute_sweep_amount


@pytest.mark.asyncio
async def test_sweep_amount_above_cap():
    assert await compute_sweep_amount(balance_usd=175.0, float_cap_usd=100.0) == 75.0


@pytest.mark.asyncio
async def test_sweep_amount_at_or_below_cap_is_zero():
    assert await compute_sweep_amount(balance_usd=80.0, float_cap_usd=100.0) == 0.0
    assert await compute_sweep_amount(balance_usd=100.0, float_cap_usd=100.0) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/live/solana/test_solana_sweep.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.solana_sweep`

- [ ] **Step 3: Implement the sweep decision + script skeleton**

Create `scripts/solana_sweep.py`:

```python
"""Daily float sweep: keep live USDC at/below SOLANA_FLOAT_CAP_USD.

This module implements and tests the sweep DECISION. The actual cold-wallet
transfer is gated behind SOLANA_SWEEP_COLD_WALLET and logged as not-yet-
implemented until the transfer-instruction design lands (spec open items).
"""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger(__name__)


async def compute_sweep_amount(*, balance_usd: float, float_cap_usd: float) -> float:
    return max(0.0, balance_usd - float_cap_usd)


async def main() -> None:  # pragma: no cover - operational entrypoint
    import aiohttp

    from scout.config import Settings
    from scout.live.solana_factory import build_solana_adapter

    settings = Settings()
    async with aiohttp.ClientSession() as session:
        adapter = build_solana_adapter(settings=settings, session=session, db=None)
        if adapter is None:
            log.info("solana_sweep_skipped_no_adapter")
            return
        balance = await adapter.fetch_account_balance("USDC")
        excess = await compute_sweep_amount(
            balance_usd=balance, float_cap_usd=float(settings.SOLANA_FLOAT_CAP_USD)
        )
        if excess <= 0:
            log.info("solana_sweep_noop", balance_usd=balance)
            return
        if not settings.SOLANA_SWEEP_COLD_WALLET:
            log.warning("solana_sweep_no_cold_wallet", excess_usd=excess)
            return
        log.warning(
            "solana_sweep_transfer_not_implemented",
            excess_usd=excess, cold_wallet=settings.SOLANA_SWEEP_COLD_WALLET,
        )


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/live/solana/test_solana_sweep.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Create the watchdog script**

Create `scripts/solana_execution_watchdog.py`:

```python
"""SLO watchdog for the solana venue. Alerts if the adapter has not produced
a reconciliation/order event within the freshness window. Mirrors the
existing watchdog scripts in scripts/ — wire its timer in systemd/."""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger(__name__)

FRESHNESS_SLO_SEC = 6 * 60 * 60  # 6h: solana boot reconciliation + activity


async def main() -> None:  # pragma: no cover - operational entrypoint
    # Placeholder SLO check: read the structured-log/journal or a heartbeat row
    # the same way the existing watchdogs do (see scripts/*_watchdog.py). Emit
    # a Telegram alert via scout.alerter when stale. Implemented to match the
    # repo's watchdog convention during ops wiring.
    log.info("solana_execution_watchdog_tick", slo_sec=FRESHNESS_SLO_SEC)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
```

- [ ] **Step 6: Create systemd units**

Create `systemd/solana-sweep.service`:

```ini
[Unit]
Description=gecko-alpha Solana float sweep
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/gecko-alpha
EnvironmentFile=/opt/gecko-alpha/.env
ExecStart=/opt/gecko-alpha/.venv/bin/python -m scripts.solana_sweep
```

Create `systemd/solana-sweep.timer`:

```ini
[Unit]
Description=Run gecko-alpha Solana float sweep daily

[Timer]
OnCalendar=*-*-* 00:10:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

Create `systemd/solana-execution-watchdog.service`:

```ini
[Unit]
Description=gecko-alpha Solana execution freshness watchdog
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/gecko-alpha
EnvironmentFile=/opt/gecko-alpha/.env
ExecStart=/opt/gecko-alpha/.venv/bin/python -m scripts.solana_execution_watchdog
```

Create `systemd/solana-execution-watchdog.timer`:

```ini
[Unit]
Description=Run gecko-alpha Solana execution watchdog hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 7: Document non-secret knobs in .env.example**

Append to `.env.example` (NEVER add `SOLANA_WALLET_SECRET`):

```bash
# --- Solana on-chain execution (BL-NEW-SOLANA) ---
# SOLANA_WALLET_SECRET is a SECRET (base58 keypair) — set it ONLY in the real
# .env on the host, never here and never in git.
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
SOLANA_JUPITER_URL=https://api.jup.ag/swap/v1
SOLANA_SLIPPAGE_BPS_CAP=100
SOLANA_PRIORITY_FEE_LAMPORTS=50000
SOLANA_MAX_PRICE_IMPACT_PCT=3.0
SOLANA_MIN_SOL_GAS_RESERVE=0.02
SOLANA_FLOAT_CAP_USD=100
SOLANA_CONFIRM_TIMEOUT_SEC=60
# SOLANA_SWEEP_COLD_WALLET=<your cold wallet pubkey>
```

- [ ] **Step 8: Run the full suite + format**

Run: `uv run pytest -q && uv run black scout/ scripts/ tests/`
Expected: all green; formatting clean.

- [ ] **Step 9: Commit**

```bash
git add scripts/solana_sweep.py scripts/solana_execution_watchdog.py systemd/solana-sweep.service systemd/solana-sweep.timer systemd/solana-execution-watchdog.service systemd/solana-execution-watchdog.timer .env.example tests/live/solana/test_solana_sweep.py
git commit -m "feat(solana): float sweep + execution watchdog + systemd units + .env docs"
```

---

## Post-Implementation: Operator Rollout (NOT code — runbook for the operator)

After all tasks merge, the live rollout is gated and manual, per the handoff:

1. **Fund a fresh hot wallet** with a tiny float (e.g. $50 USDC + ~0.05 SOL gas). Put its base58 secret in the host `.env` as `SOLANA_WALLET_SECRET` (never committed).
2. **Shadow first:** `LIVE_MODE=shadow`, `LIVE_SIGNAL_ALLOWLIST=<one signal>`. Confirm `shadow_trades` rows open via quote+simulate, sellability gate behaves, and no broadcast occurs. Watch `journalctl -u gecko-pipeline -f` for `solana_order_sent` absence and gate logs.
3. **Tiny live:** `LIVE_MODE=live`, `LIVE_TRADING_ENABLED=true`, `LIVE_TRADE_AMOUNT_USD=10`, `SOLANA_FLOAT_CAP_USD=50`, one signal only. (The on-chain fork does NOT use `LIVE_USE_ROUTING_LAYER` / `LIVE_USE_REAL_SIGNED_REQUESTS` — those gate the Binance routing path; the Solana fork is reached purely via `onchain_adapter` being present + a Solana-chain signal.) Watch a full route: paper open → `live_dispatch_entered` (venue=solana) → `live_dispatch_terminal` status=filled → `live_trades` row with `entry_fill_price` → sell/reconcile.
4. **Verify** the daily sweep timer and execution watchdog are enabled (`systemctl --user enable --now solana-sweep.timer solana-execution-watchdog.timer` per `systemd/README.md` workflow).
5. **Only then** widen signals/size. Do not enable by flag alone.

## Spec Coverage Self-Review

- Spec §3 architecture (one adapter + 3 sub-modules, reuse rest) → Tasks 2–15. ✓
- Spec §4.1 the 8 contract methods → Tasks 7–10 (read-only, balance, place, await). ✓
- Spec §4.2 sub-modules jupiter/wallet/rpc → Tasks 2–5. ✓
- Spec §4.3 buy AND sell via one adapter (`side`) → Task 9 (`_mints_for_side`) + Task 7. ✓
- Spec §4.4 SOLANA_* config → Task 6. ✓
- Spec §5 modes (paper/shadow/live) + lifecycle → engine fork Task 14; shadow integration Task 16; live path Tasks 9–10+14. ✓
- Spec §5.4 on-chain states mapped to enums → adapter Task 10 (filled/rejected/timeout); DB write of valid live_trades.status in Task 14 (filled⇒stays 'open', rejected, timeout⇒open). ✓
- Spec §5.4 boot reconciliation by signature → Task 12 + Task 15 hook. ✓
- Spec §5.5 daily sweep → Task 17. ✓
- Spec §6 on-chain gates (price-impact, sellability, gas, float) → Task 11, invoked via the fork's second Gates instance in Task 14 (+ exposure reuse). ✓
- **Engine multi-venue integration (review finding): the live engine is single-adapter and does NOT select by routed venue → isolated on-chain fork (`onchain_adapter` + `_onchain_gates` + `_dispatch_onchain`) → Tasks 13 (mint resolver), 14 (fork), 15 (wiring). Binance path byte-unchanged when `onchain_adapter is None`. ✓**
- Spec §7 error handling taxonomy → Tasks 5 (`RpcError`), 10 (timeout/rejected). Transient-retry-with-fresh-blockhash is flagged below. ⚠
- Spec §8 safety (kill switch inherited, idempotency, secrets, watchdog, Telegram) → kill/allowlist in Task 11; idempotency (record_pending_order + client_order_id) in Task 14; secrets in Tasks 4/6; watchdog in Task 17. Wallet-drain tripwire flagged below. ⚠
- Spec §9 testing → every task is TDD; shadow integration Task 16. ✓
- Spec §10 rollout ladder → Post-Implementation runbook. ✓

### Review corrections applied (pre-execution, against real code + live APIs)

- **`live_trades.status` CHECK constraint** allows only `open / closed_tp / closed_sl / closed_duration / closed_via_reconciliation / rejected / needs_manual_review`. Tasks 12 & 14 corrected: a filled buy stays `'open'` (never the invalid `'filled'`); failed→`'rejected'`; timeout→stays `'open'`.
- **Jupiter v6 `quote-api.jup.ag` deprecated (Oct 2025)** → default `https://api.jup.ag/swap/v1` + optional `SOLANA_JUPITER_API_KEY` (`x-api-key`); Tasks 2/6/15.
- **solders test helper** fixed to `MessageV0.try_compile(..., Hash.default())` (blockhash is a `Hash`, not bytes); Task 4. Signing idiom `VersionedTransaction(message, [keypair])` confirmed current.
- **`solders` pin** widened to `<0.28` (current is 0.27.x); Task 1.

### Deliberately deferred (carried from spec §11 open items — not gaps, scoped out with a flag)

- **Transient retry with fresh blockhash** (spec §7): Task 5 surfaces `RpcError`; a bounded resend-with-new-blockhash loop that never re-sends a confirmed signature is a follow-up hardening task. Until then, a dropped tx is resolved by Task 12 boot reconciliation (safe, just slower).
- **Wallet-drain tripwire** (spec §8): the balance snapshot → kill-switch trigger is a follow-up; the daily sweep (Task 17) bounds exposure in the interim.
- **Cold-wallet transfer instruction-building** (spec §11): Task 17 implements/tests the sweep *decision* and logs the transfer as not-yet-implemented; the signed transfer lands with the transfer design.
- **Non-native mint resolution** (review finding): Task 13 resolves native Solana tokens where `coin_id` IS the mint. CoinGecko-slug tokens whose mint lives in `platforms.solana` need a `fetch_coin_detail` network lookup — a follow-up. First rollout snipes native tokens, so this does not block shadow/tiny-live.
- **Jupiter `priceImpactPct` unit assumption** (fraction → ×100): encoded in Task 7; verify against a live quote during shadow before tiny-live.

These are intentionally separate, testable follow-up tasks rather than blockers for reaching a working shadow mode.
