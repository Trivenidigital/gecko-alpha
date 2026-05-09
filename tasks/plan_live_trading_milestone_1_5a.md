**New primitives introduced:** New module `scout/live/binance_signing.py` exposing `sign_request(secret, params) → (params_with_signature, signature_header)` (HMAC-SHA256 over query-string). New `BinanceSpotAdapter._request(method, path, *, params, headers, signed)` core method — `_http_get` is refactored to delegate (per R1-C1 plan-stage finding); `_signed_get` + `_signed_post` are thin wrappers over `_request` that add HMAC + timestamp + recvWindow + auth-error taxonomy. New `BinanceAuthError` (auth failures `-2014`/`-2015`/`-1021`) and `BinanceIPBanError` (HTTP 418 — distinct from 429 transient rate-limit) exception classes. Runtime bodies for the 3 ABC stubs introduced in M1: `fetch_account_balance(asset)`, `place_order_request(request)` (idempotency-aware via `idempotency.make_client_order_id` + pre-retry `lookup_existing_order_id` + `IntegrityError` race handler + Binance `-2010` duplicate handling per R2-I2), `await_fill_confirmation(*, venue_order_id, client_order_id, timeout_sec)` (pre-loop symbol resolution via single `SELECT pair FROM live_trades WHERE client_order_id = ?` per R1-C4; venue-side polling with adaptive backoff; `fill_slippage_bps` computed at confirmation — semantic clarified per R2-C2: "drift-inclusive proxy" measuring `(fill_price/mid_at_entry - 1) * 10000` where `mid_at_entry` was sampled at `place_order_request` start; includes ~200-500ms of market drift between quote + fill, averages to ~0 across ≥30 samples in V1 approval-removal gate; column name `fill_slippage_bps` retained from M1 migration to avoid schema churn). New Gate 10 runtime body in `scout/live/gates.py` (top-of-module `from scout.live.balance_gate import check_sufficient_balance` per R1-I3). New main.py startup smoke check (5s timeout) gated on `LIVE_MODE='live'` — replaces M1's NotImplementedError. Per R2-C1 (systemd restart-loop fix): plan ALSO requires `gecko-pipeline.service` to have `RestartSec=30s` + `StartLimitBurst=3` — verified in new Task 7.5 BEFORE any LIVE_MODE flip. New `LIVE_USE_REAL_SIGNED_REQUESTS: bool = False` Settings flag (R2-I4 emergency-revert path) — when False, the 3 runtime bodies fall back to NotImplementedError as before; default False so post-deploy behavior is identical to M1 unless operator explicitly opts in. New `LIVE_STARTUP_NOTIFICATION_MIN_INTERVAL_SEC: int = 300` Settings field — rate-limits Telegram startup notification to ≤1 per 5 min (R2-I5 anti-spam under restart loop). Telegram approval gateway runtime hook NOT added (Layer 4 enforcement = M1.5b).

**Plan-stage 2-reviewer pass folded 2026-05-09** — R1 (structural/code) + R2 (strategy/blast-radius). 6 R1 critical + 2 R2 critical + 9 important + 4 minor. All folded. See "AMENDMENTS" section at end of file for itemized resolution. Net plan delta: +1 task (Task 1.5: HTTP core refactor), +1 task (Task 7.5: systemd verification), 2 new Settings fields, expanded test matrix (-1021 timestamp / IntegrityError race / -2010 dedup / mid_at_entry NULL / partial-fill terminal / POST 5xx retry).

# Live Trading Milestone 1.5a Implementation Plan — Binance REST Signing + Runtime Bodies

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Binance REST HMAC-SHA256 signing + runtime bodies for the 3 NotImplementedError stubs introduced in M1 (`fetch_account_balance`, `place_order_request`, `await_fill_confirmation`). Ship the balance_gate runtime check (Gate 10) replacing M1's NotImplementedError. Update `scout/main.py` startup guard so `LIVE_MODE='live'` boots successfully when properly configured (LIVE_TRADING_ENABLED=True + Binance creds + balance_gate smoke check passes).

**Architecture:** Native HMAC-SHA256 signing (per `findings_ccxt_verification_2026_05_08.md` architectural decision to keep BL-055 native for Binance, NOT use CCXT). Signing primitive lives in dedicated module `scout/live/binance_signing.py` so it can be re-tested independent of aiohttp. `BinanceSpotAdapter` gets `_signed_get` + `_signed_post` methods that reuse the existing `_http_get` rate-limit + retry taxonomy. Runtime bodies use `idempotency.py` helpers for the dedup contract on `place_order_request`. `await_fill_confirmation` polls `/api/v3/order` with adaptive backoff (200ms → 500ms → 1s → 2s) until terminal state or timeout. Slippage computed and written at fill confirmation time. Tests use `aioresponses` for HTTP mocking (project standard) + dedicated signing tests against Binance's published HMAC fixtures.

**Tech Stack:** Python 3.12, aiohttp, hmac + hashlib (stdlib for HMAC-SHA256), aiosqlite, pydantic v2 BaseSettings + field_validator, pytest-asyncio (auto mode), aioresponses (HTTP mock), structlog (PrintLoggerFactory — tests use `structlog.testing.capture_logs`, NOT pytest caplog), black formatting.

**Test reference snippets omit `_REQUIRED` for brevity** — same convention as M1 plan: `Settings(_env_file=None)` calls in this plan must add `**_REQUIRED` (or use `tests/conftest.py:60` `settings_factory` fixture) to satisfy the 3 mandatory fields (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`). Module-level convention from `tests/test_config.py:11`: `_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")`.

**Total scope:** ~50-60 steps across 9 tasks. Smaller than M1's ~80 steps because no schema migrations + no new SQL views + no new tables — purely runtime wiring.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/live/binance_signing.py` | **Create** | `sign_request(secret, params) → tuple[dict, str]` — HMAC-SHA256 over query string. Pure function, no I/O. |
| `scout/live/binance_adapter.py` | Modify | Add `_signed_get` + `_signed_post` helpers. Replace 3 NotImplementedError stubs with runtime bodies. |
| `scout/live/gates.py` | Modify | Replace Gate 10 NotImplementedError with balance_gate runtime call. |
| `scout/main.py` | Modify | Replace `balance gate not wired` NotImplementedError with smoke check + Layer 1 master kill guard. |
| `tests/test_live_binance_signing.py` | **Create** | HMAC fixture tests (Binance docs example), signature-on-existing-params test, key-order independence test. |
| `tests/test_live_binance_adapter_signed.py` | **Create** | aioresponses-mocked signed_get/signed_post + 3 runtime body tests. |
| `tests/test_live_gates_balance_runtime.py` | **Create** | Gate 10 runtime: insufficient/sufficient/adapter-failure cases. |
| `tests/test_live_main_startup_balance_smoke.py` | **Create** | main.py startup: smoke-check pass / fail / Layer-1-master-kill cases. |
| `tests/test_live_balance_gate.py` | Modify | Extend with adapter-error mapping cases (transient → passed=False). |
| `tests/test_live_idempotency.py` | Modify | Extend with concurrent-INSERT race test (UNIQUE INDEX backstop). |

**Schema versions reserved:** none — M1.5a does NOT introduce new migrations.

---

## Task 0: Setup — branch + prerequisite verification

- [ ] **Step 1: Verify branch**

```bash
git branch --show-current
# Expected: feat/live-trading-m1-5a (created already by parent; if not, branch off origin/master)
```

- [ ] **Step 2: Verify M1 prereqs are present**

```bash
ls scout/live/balance_gate.py            # Created in M1 Task 8
ls scout/live/idempotency.py             # Created in M1 Task 12
ls scout/live/adapter_base.py            # M1 Task 5 ABC additive reshape
grep -c "place_order_request\|await_fill_confirmation\|fetch_account_balance" scout/live/binance_adapter.py
# Expected: 3+ (the stubs to replace)
grep -c "from scout.live.binance_signing" scout/live/binance_adapter.py
# Expected: 0 (signing module not yet imported — will land in Task 2)
```

- [ ] **Step 3: Verify Binance API base + creds plumbing — HARD PREREQ (R1-M4)**

```bash
grep -E "_BASE_URL|api.binance.com|BINANCE_API_KEY" scout/live/binance_adapter.py | head -5
grep "BINANCE_API_KEY\|BINANCE_API_SECRET" scout/config.py | head -3
```

**If creds plumbing is missing, STOP and report BLOCKED.** Plan does NOT introduce Settings fields for BINANCE_API_KEY / BINANCE_API_SECRET — they must already exist in `scout/config.py` from BL-055. If absent, fix that first as a separate ticket. (R1-M4: previous wording was "flag and continue" — this hardens it to fail-fast since Task 2 cannot proceed without these.)

- [ ] **Step 4: Add 2 new Settings fields**

In `scout/config.py`, add (mirror the BL-NEW-LIVE-HYBRID block from M1):

```python
    # M1.5a — gates the signed-endpoint runtime codepath. When False (default),
    # the 3 ABC runtime bodies fall back to NotImplementedError for emergency
    # revert without git revert. Operator flips True after balance smoke check
    # passes on testnet (R2-I4).
    LIVE_USE_REAL_SIGNED_REQUESTS: bool = False

    # M1.5a — rate-limit the LIVE_TRADING_ENABLED=True startup Telegram alert
    # to ≤1 per N seconds. Prevents alert-spam during systemd restart loops
    # if the balance smoke check is flapping (R2-I5).
    LIVE_STARTUP_NOTIFICATION_MIN_INTERVAL_SEC: int = 300
```

Plus a small validator on the second field (`> 0`).

- [ ] **Step 5: Failing test for the 2 new Settings**

Add to `tests/test_live_master_kill.py`:

```python
def test_live_use_real_signed_requests_defaults_off(self):
    assert Settings(_env_file=None, **_REQUIRED).LIVE_USE_REAL_SIGNED_REQUESTS is False


def test_live_startup_notification_min_interval_default(self):
    assert Settings(_env_file=None, **_REQUIRED).LIVE_STARTUP_NOTIFICATION_MIN_INTERVAL_SEC == 300


def test_live_startup_notification_min_interval_must_be_positive(self):
    with pytest.raises(ValueError, match="must be > 0"):
        Settings(_env_file=None, **_REQUIRED, LIVE_STARTUP_NOTIFICATION_MIN_INTERVAL_SEC=0)
```

- [ ] **Step 6: Run + commit**

```bash
uv run --native-tls pytest tests/test_live_master_kill.py -v
git add scout/config.py tests/test_live_master_kill.py
git commit -m "feat(live-m1.5a): 2 new Settings fields (revert flag + Telegram rate-limit) — Task 0"
```

---

## Task 1: HMAC signing primitive (`scout/live/binance_signing.py`)

**Files:**
- Create: `scout/live/binance_signing.py`
- Test: `tests/test_live_binance_signing.py` (NEW)

- [ ] **Step 1: Failing tests**

Create `tests/test_live_binance_signing.py`:

```python
"""BL-NEW-LIVE-HYBRID M1.5a: Binance HMAC-SHA256 signing primitive."""

from __future__ import annotations

from scout.live.binance_signing import sign_request


def test_sign_request_known_fixture():
    """Binance docs HMAC fixture (api.binance.com SIGNED endpoints):
    https://binance-docs.github.io/apidocs/spot/en/#signed-trade-and-user_data-endpoint-security

    apiKey: vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A
    secretKey: NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j
    queryString: symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559
    expected signature: c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71
    """
    params = {
        "symbol": "LTCBTC",
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "1",
        "price": "0.1",
        "recvWindow": "5000",
        "timestamp": "1499827319559",
    }
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    signed_params, signature = sign_request(secret, params)
    assert signature == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert signed_params["signature"] == signature
    # Original params preserved + signature key added
    for k, v in params.items():
        assert signed_params[k] == v


def test_sign_request_preserves_param_order():
    """The signature is computed from the SAME insertion order as
    params.items(). If we accidentally re-sort keys, the server's
    signature won't match. Lock the contract."""
    params = {"b": "2", "a": "1", "timestamp": "1"}
    secret = "test"
    signed_params, signature = sign_request(secret, params)
    # signed_params must preserve insertion order of `params` then append signature
    keys = list(signed_params.keys())
    assert keys == ["b", "a", "timestamp", "signature"]


def test_sign_request_does_not_mutate_input():
    """sign_request must return a fresh dict — the caller's dict is read-only."""
    params = {"timestamp": "1"}
    secret = "test"
    sign_request(secret, params)
    assert "signature" not in params
```

- [ ] **Step 2: Run — expect 3 FAILs**

- [ ] **Step 3: Implement**

Create `scout/live/binance_signing.py`:

```python
"""BL-NEW-LIVE-HYBRID M1.5a: Binance HMAC-SHA256 signing primitive.

Pure function — no I/O, no aiohttp. Tested against Binance's published
HMAC fixture independently of the adapter layer.

Per Binance SIGNED endpoint security spec:
https://binance-docs.github.io/apidocs/spot/en/#signed-trade-and-user_data-endpoint-security

The signature is computed as HMAC-SHA256 of the query string (URL-encoded
key=value joined by &), keyed by the operator's secret. The result is
appended as `signature=<hex>` and sent either:
  - GET: as a final query parameter (this implementation's choice)
  - POST: as a final form field

Both work; we use query-string injection for both methods so the same
helper covers GET + POST.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from urllib.parse import urlencode


def sign_request(
    secret: str, params: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Sign a Binance request.

    Args:
        secret: The operator's BINANCE_API_SECRET (raw string, NOT URL-encoded).
        params: Request parameters in insertion order. Must already include
            `timestamp` and (recommended) `recvWindow`. Must NOT include
            `signature` — this function appends it.

    Returns:
        (signed_params, signature_hex) where:
        - signed_params is a NEW dict with the original params (insertion-
          order preserved) plus a final `signature` key.
        - signature_hex is the hex-encoded HMAC-SHA256 digest.

    Notes:
        - Insertion order matters because urlencode() is order-preserving
          on Python 3.7+ dict iteration. Caller is responsible for ordering.
        - Caller's dict is NOT mutated — function returns a fresh dict.
    """
    # Build query string in EXACT input order (don't sort).
    query_string = urlencode(params)
    digest = hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    signed_params = dict(params)
    signed_params["signature"] = digest
    return signed_params, digest
```

- [ ] **Step 4: Run — expect 3 PASS**

- [ ] **Step 5: Commit**

```bash
git add scout/live/binance_signing.py tests/test_live_binance_signing.py
git commit -m "feat(live-m1.5a): Binance HMAC-SHA256 signing primitive (BL-NEW-LIVE-HYBRID M1.5a Task 1)"
```

---

## Task 1.5: Refactor `_http_get` → `_request` core (R1-C1)

**Files:**
- Modify: `scout/live/binance_adapter.py`
- Test: extend existing `tests/live/test_binance_adapter.py` (re-runs against the refactored core)

**Why this task exists:** R1-C1 plan-stage finding caught that the original M1.5a plan duplicated `_http_get`'s retry loop, weight governor, and 429 handling inside `_signed_get` + `_signed_post`. Drift between the two retry paths is guaranteed unless they share a core. Task 1.5 introduces a shared `_request` method; Task 2 then implements `_signed_get`/`_signed_post` as thin wrappers.

- [ ] **Step 1: Read existing `_http_get` (line 90-159) to understand the retry shape**

```bash
grep -A 70 "async def _http_get" scout/live/binance_adapter.py | head -80
```

- [ ] **Step 2: Extract `_request(method, path, *, params, headers, signed)` core**

```python
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        signed: bool = False,  # signed callers pre-inject signature; flag exists
                                # for future telemetry / weight differentiation
    ) -> dict[str, Any]:
        """Central Binance HTTP. Retry taxonomy + weight governor +
        rate-limit gate + signed-vs-unsigned error mapping.

        Returns parsed JSON body on 200. Returns sentinel `{"__code": -1121}`
        on Binance 400+code=-1121 (unknown symbol — preserved from M1
        contract). Raises:
          - BinanceAuthError on -2014/-2015/-1021 (signed callers only)
          - BinanceIPBanError on HTTP 418 (R1-I1)
          - VenueTransientError on 5xx, network, max retries
        """
        await self._rate_limit_gate.wait()
        url = f"{_BASE_URL}{path}"
        last_exc: Exception | None = None
        for attempt in range(len(_BACKOFFS) + 1):
            try:
                if method == "GET":
                    cm = self._session.get(url, params=params, headers=headers)
                elif method == "POST":
                    cm = self._session.post(url, params=params, headers=headers)
                else:
                    raise ValueError(f"Unsupported method: {method}")
                async with cm as resp:
                    weight = int(resp.headers.get("X-MBX-USED-WEIGHT-1M", 0))
                    await self._update_weight_governor(weight)
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 418:
                        # IP-ban — distinct from 429 (R1-I1)
                        body = await resp.json()
                        raise BinanceIPBanError(
                            f"{method} {path}: 418 IP-banned: {body}"
                        )
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        await asyncio.sleep(
                            int(retry_after)
                            if retry_after
                            else (
                                _BACKOFFS[attempt]
                                if attempt < len(_BACKOFFS)
                                else 8
                            )
                        )
                        continue
                    body = await resp.json()
                    code = body.get("code") if isinstance(body, dict) else None
                    if code == -1121:
                        # Unknown symbol — preserve M1 sentinel contract
                        return {"__code": -1121}
                    if signed and code in (-2014, -2015, -1021):
                        raise BinanceAuthError(
                            f"{method} {path} failed code={code} msg={body.get('msg')!r}"
                        )
                    if signed and code == -2010:
                        # Duplicate clientOrderId (R2-I2 dedup race) — surface
                        # as a typed exception so caller can recover via
                        # origClientOrderId lookup instead of treating as
                        # transient. Caller is responsible for re-querying
                        # the existing order.
                        raise BinanceDuplicateOrderError(
                            f"duplicate newClientOrderId: {body.get('msg')!r}"
                        )
                    if resp.status >= 500:
                        if attempt < len(_BACKOFFS):
                            await asyncio.sleep(_BACKOFFS[attempt])
                            continue
                    raise VenueTransientError(
                        f"{method} {path}: {resp.status} {body}"
                    )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as exc:
                last_exc = exc
                if attempt < len(_BACKOFFS):
                    await asyncio.sleep(_BACKOFFS[attempt])
                    continue
                break
        if last_exc:
            raise VenueTransientError(f"{method} {path}: {last_exc}") from last_exc
        raise VenueTransientError(f"{method} {path}: max retries exceeded")
```

Add new exception classes near the top of binance_adapter.py:

```python
class BinanceAuthError(Exception):
    """-2014/-2015/-1021 — never retry."""


class BinanceIPBanError(Exception):
    """HTTP 418 — distinct from 429; back off minutes-to-hours."""


class BinanceDuplicateOrderError(Exception):
    """-2010 — duplicate newClientOrderId; caller recovers via origClientOrderId lookup."""
```

- [ ] **Step 3: Refactor `_http_get` to delegate**

```python
    async def _http_get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Public unsigned GET — back-compat wrapper over `_request`."""
        return await self._request("GET", path, params=params, signed=False)
```

- [ ] **Step 4: Run existing BL-055 tests — expect all pass (no regression)**

```bash
uv run --native-tls pytest tests/live/test_binance_adapter.py -v
```

(May hit Windows OpenSSL crash — that's pre-existing, not regression.)

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(binance_adapter): extract _request core for signed-vs-unsigned share — Task 1.5"
```

---

## Task 2: `_signed_get` + `_signed_post` helpers on BinanceSpotAdapter

**Files:**
- Modify: `scout/live/binance_adapter.py`
- Test: `tests/test_live_binance_adapter_signed.py` (NEW)

- [ ] **Step 1: Failing tests**

Create `tests/test_live_binance_adapter_signed.py` initial set:

```python
"""BL-NEW-LIVE-HYBRID M1.5a: BinanceSpotAdapter signed-request helpers."""

from __future__ import annotations

import time
from decimal import Decimal

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.live.binance_adapter import BinanceSpotAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def _settings():
    return Settings(
        _env_file=None,
        BINANCE_API_KEY="testkey",
        BINANCE_API_SECRET="testsecret",
        **_REQUIRED,
    )


@pytest.mark.asyncio
async def test_signed_get_appends_timestamp_and_signature():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/account",
            payload={"balances": []},
        )
        await adapter._signed_get("/api/v3/account", params={})
        # Inspect the request URL to verify timestamp + signature were appended
        recorded = list(m.requests.values())[0][0]
        url = str(recorded.kwargs.get("url", recorded.url))
        # url query string must contain timestamp= AND signature=
        assert "timestamp=" in url
        assert "signature=" in url
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_get_includes_api_key_header():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/account",
            payload={"balances": []},
        )
        await adapter._signed_get("/api/v3/account", params={})
        recorded = list(m.requests.values())[0][0]
        headers = recorded.kwargs.get("headers", {})
        assert headers.get("X-MBX-APIKEY") == "testkey"
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_post_form_encoded():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.post(
            "https://api.binance.com/api/v3/order",
            payload={"orderId": 12345, "status": "NEW"},
        )
        result = await adapter._signed_post(
            "/api/v3/order",
            params={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": "10",
                "newClientOrderId": "gecko-1-abcd1234",
            },
        )
        assert result["orderId"] == 12345
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_get_raises_on_signature_invalid():
    """Binance error -2014 (Signature for input invalid) → should raise
    a clear exception so caller knows it's auth, not transient."""
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/account",
            status=400,
            payload={"code": -2014, "msg": "Signature for input invalid"},
        )
        with pytest.raises(Exception) as excinfo:
            await adapter._signed_get("/api/v3/account", params={})
        # Must surface the -2014 code in the exception so logs explain
        assert "2014" in str(excinfo.value) or "Signature" in str(excinfo.value)
    await adapter.close()
```

- [ ] **Step 2: Run — expect 4 FAILs**

- [ ] **Step 3: Implement `_signed_get` + `_signed_post` as thin wrappers over `_request` (R1-C1, R1-C2)**

`BinanceAuthError`, `BinanceIPBanError`, `BinanceDuplicateOrderError` are added in Task 1.5. `_request` is the shared core. `_signed_get` and `_signed_post` only handle signature injection + sign-specific parameter setup; retry/weight/418/429 all live in `_request`.

```python
    async def _signed_get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Signed GET — adds HMAC + timestamp + recvWindow + X-MBX-APIKEY.
        All retry/weight/auth-error logic lives in `_request`."""
        from scout.live.binance_signing import sign_request

        body_params = dict(params or {})
        body_params["timestamp"] = int(time.time() * 1000)
        body_params["recvWindow"] = 5000
        signed_params, _sig = sign_request(
            self._settings.BINANCE_API_SECRET, body_params
        )
        headers = {"X-MBX-APIKEY": self._settings.BINANCE_API_KEY}
        return await self._request(
            "GET", path, params=signed_params, headers=headers, signed=True
        )


    async def _signed_post(
        self, path: str, *, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Signed POST — same signature scheme as _signed_get; full retry +
        5xx handling inherited from `_request` (R1-C2 fix — original plan
        had no retry for POST, leaking 503s as fatal)."""
        from scout.live.binance_signing import sign_request

        body_params = dict(params)
        body_params["timestamp"] = int(time.time() * 1000)
        body_params["recvWindow"] = 5000
        signed_params, _sig = sign_request(
            self._settings.BINANCE_API_SECRET, body_params
        )
        headers = {"X-MBX-APIKEY": self._settings.BINANCE_API_KEY}
        return await self._request(
            "POST", path, params=signed_params, headers=headers, signed=True
        )
```

The 3 exception classes (`BinanceAuthError`, `BinanceIPBanError`, `BinanceDuplicateOrderError`) ALREADY exist near the top of `binance_adapter.py` from Task 1.5 — DO NOT add them again here.

- [ ] **Step 3.5: Add tests for the auth-error matrix expansion (R1 test gap)**

Add to `tests/test_live_binance_adapter_signed.py`:

```python
@pytest.mark.asyncio
async def test_signed_get_raises_on_timestamp_drift():
    """Binance error -1021 (Timestamp out-of-recvWindow) — common in prod
    on clock skew; must surface as BinanceAuthError (not retry)."""
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/account",
            status=400,
            payload={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow"},
        )
        from scout.live.binance_adapter import BinanceAuthError
        with pytest.raises(BinanceAuthError) as excinfo:
            await adapter._signed_get("/api/v3/account", params={})
        assert "1021" in str(excinfo.value)
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_endpoint_raises_ip_ban_on_418():
    """HTTP 418 → BinanceIPBanError (distinct from 429 retry-able)."""
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/account",
            status=418,
            payload={"code": -1003, "msg": "Way too much request weight used"},
        )
        from scout.live.binance_adapter import BinanceIPBanError
        with pytest.raises(BinanceIPBanError):
            await adapter._signed_get("/api/v3/account", params={})
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_post_retries_on_5xx():
    """POST must tolerate transient 5xx (R1-C2 fix). One 503 then 200 →
    success, not crash."""
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.post(
            "https://api.binance.com/api/v3/order",
            status=503,
            payload={"code": -1000, "msg": "transient"},
        )
        m.post(
            "https://api.binance.com/api/v3/order",
            payload={"orderId": 12345, "status": "NEW"},
        )
        result = await adapter._signed_post(
            "/api/v3/order",
            params={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": "10",
                "newClientOrderId": "gecko-1-abcd1234",
            },
        )
        assert result["orderId"] == 12345
    await adapter.close()
```

**Windows OpenSSL test gotcha note (R1-I5):** these aiohttp-based tests will not run on Windows due to pre-existing OpenSSL Applink crash. They run on CI Linux runners. For local-Windows test coverage, the implementer adds a parallel set of source-text-inspection tests OR uses `aioresponses` with a stub-session pattern. Pre-existing convention in `tests/test_live_balance_gate.py:13-26` uses stub-adapter pattern — mirror that for cross-platform smoke tests on the runtime-body call paths.

- [ ] **Step 4: Run — expect 4 PASS**

- [ ] **Step 5: Commit**

```bash
git add scout/live/binance_adapter.py tests/test_live_binance_adapter_signed.py
git commit -m "feat(live-m1.5a): _signed_get + _signed_post on BinanceSpotAdapter (BL-NEW-LIVE-HYBRID M1.5a Task 2)"
```

---

## Task 3: `fetch_account_balance` runtime body

**Files:**
- Modify: `scout/live/binance_adapter.py`
- Test: `tests/test_live_binance_adapter_signed.py` (extend)

- [ ] **Step 1: Failing tests** (extend existing file)

```python
@pytest.mark.asyncio
async def test_fetch_account_balance_returns_free_balance():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/account",
            payload={
                "balances": [
                    {"asset": "BTC", "free": "0.5", "locked": "0.0"},
                    {"asset": "USDT", "free": "1234.56", "locked": "100.0"},
                ]
            },
        )
        balance = await adapter.fetch_account_balance("USDT")
        assert balance == 1234.56
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_account_balance_returns_zero_when_asset_absent():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/account",
            payload={"balances": [{"asset": "BTC", "free": "0.5", "locked": "0"}]},
        )
        balance = await adapter.fetch_account_balance("XYZ")
        assert balance == 0.0
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_account_balance_propagates_auth_error():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/account",
            status=401,
            payload={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action"},
        )
        from scout.live.binance_adapter import BinanceAuthError
        with pytest.raises(BinanceAuthError):
            await adapter.fetch_account_balance("USDT")
    await adapter.close()
```

- [ ] **Step 2: Run — expect 3 FAILs**

- [ ] **Step 3: Replace stub in binance_adapter.py**

```python
    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        """Return free balance in `asset` (signed GET /api/v3/account)."""
        body = await self._signed_get("/api/v3/account", params={})
        balances = body.get("balances", [])
        for entry in balances:
            if entry.get("asset", "").upper() == asset.upper():
                return float(entry.get("free", "0"))
        return 0.0
```

- [ ] **Step 4: Run — expect 3 PASS**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(live-m1.5a): fetch_account_balance runtime body (BL-NEW-LIVE-HYBRID M1.5a Task 3)"
```

---

## Task 4: `place_order_request` runtime body (idempotency-aware)

**Plan-stage 2-reviewer fixes folded (R1-C3, R1-C6, R1-I2, R2-I2):** see AMENDMENTS A4 at end of file. Implementer MUST:
- Wrap `record_pending_order(...)` in `try/except sqlite3.IntegrityError` and re-call `lookup_existing_order_id` on collision (R1-C3)
- Reject empty/missing `orderId` from Binance response with `VenueTransientError` — never persist `entry_order_id=""` (R1-C6)
- Acquire `db._txn_lock` around the `UPDATE live_trades SET entry_order_id` (R1-I2)
- Catch `BinanceDuplicateOrderError` from `_signed_post` (-2010 means our retry collided with a successful Binance submit on the previous attempt) — recover by signed GET on `origClientOrderId`, extract `orderId`, persist, return (R2-I2)
- Gate the entire signed codepath behind `if self._settings.LIVE_USE_REAL_SIGNED_REQUESTS:` — when False, raise NotImplementedError as before (R2-I4 emergency-revert)

**Files:**
- Modify: `scout/live/binance_adapter.py`
- Test: `tests/test_live_binance_adapter_signed.py` (extend)

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_place_order_request_dedup_returns_existing_order_id(tmp_path):
    """If a live_trades row already has the client_order_id, return its
    venue_order_id without hitting Binance."""
    from scout.db import Database
    from scout.live.adapter_base import OrderRequest
    from scout.live.idempotency import make_client_order_id, record_pending_order

    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed paper_trades parent row + a live_trades row with entry_order_id set
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES ('btc-tok', 'BTC', 'btc', 'ethereum', 'first_signal', '{}',
                   100, 50, 0.5, 120, 80, 'open',
                   '2026-05-09T00:00:00+00:00')"""
    )
    paper_id = (await (await db._conn.execute("SELECT MAX(id) FROM paper_trades")).fetchone())[0]
    intent_uuid = "abcd1234-ef56-7890-abcd-ef0123456789"
    cid = make_client_order_id(paper_id, intent_uuid)
    live_id = await record_pending_order(
        db, client_order_id=cid, paper_trade_id=paper_id,
        coin_id="btc", symbol="BTC", venue="binance", pair="BTCUSDT",
        signal_type="first_signal", size_usd="50",
    )
    await db._conn.execute(
        "UPDATE live_trades SET entry_order_id = ? WHERE id = ?",
        ("BNX-99999", live_id),
    )
    await db._conn.commit()

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    request = OrderRequest(
        paper_trade_id=paper_id, canonical="BTC", venue_pair="BTCUSDT",
        side="buy", size_usd=50.0, intent_uuid=intent_uuid,
    )
    # Should NOT issue any HTTP call — dedup hits first
    with aioresponses() as m:
        order_id = await adapter.place_order_request(request)
        assert order_id == "BNX-99999"
        assert len(m.requests) == 0  # zero HTTP calls
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_place_order_request_first_attempt_records_then_submits(tmp_path):
    """No prior dedup row → record_pending_order writes shadow row →
    submit to Binance → returns venue_order_id."""
    from scout.db import Database
    from scout.live.adapter_base import OrderRequest

    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES ('btc-tok', 'BTC', 'btc', 'ethereum', 'first_signal', '{}',
                   100, 50, 0.5, 120, 80, 'open',
                   '2026-05-09T00:00:00+00:00')"""
    )
    paper_id = (await (await db._conn.execute("SELECT MAX(id) FROM paper_trades")).fetchone())[0]
    intent_uuid = "abcd1234-ef56-7890-abcd-ef0123456789"

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        m.post(
            "https://api.binance.com/api/v3/order",
            payload={"orderId": 88888, "status": "NEW"},
        )
        request = OrderRequest(
            paper_trade_id=paper_id, canonical="BTC", venue_pair="BTCUSDT",
            side="buy", size_usd=50.0, intent_uuid=intent_uuid,
        )
        order_id = await adapter.place_order_request(request)
        assert order_id == "88888"

    # Verify live_trades row was created with the cid
    cur = await db._conn.execute(
        "SELECT client_order_id, status FROM live_trades WHERE paper_trade_id = ?",
        (paper_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[1] == "open"
    await adapter.close()
    await db.close()
```

- [ ] **Step 2: Run — expect 2 FAILs**

- [ ] **Step 3: Replace stub**

```python
    async def place_order_request(self, request: OrderRequest) -> str:
        """Submit a market order to Binance. Returns venue_order_id.

        Idempotency: pre-checks live_trades for the client_order_id (Task
        12 / idempotency.py). If a row exists with entry_order_id set,
        returns that id without hitting Binance (replay-safe).

        On first attempt: records a 'open'-status row, submits the order,
        returns the venue's orderId. The row's entry_order_id is updated
        in await_fill_confirmation (Task 5).
        """
        from scout.live.idempotency import (
            make_client_order_id,
            lookup_existing_order_id,
            record_pending_order,
        )

        if self._db is None:
            raise RuntimeError(
                "place_order_request requires db wired into BinanceSpotAdapter"
            )

        cid = make_client_order_id(request.paper_trade_id, request.intent_uuid)
        existing = await lookup_existing_order_id(self._db, cid)
        if existing is not None:
            log.info(
                "place_order_dedup_hit",
                client_order_id=cid,
                venue_order_id=existing,
            )
            return existing

        # Capture mid_at_entry from a quick depth fetch — used by
        # await_fill_confirmation to compute slippage_bps.
        try:
            depth = await self.fetch_depth(request.venue_pair)
            mid_str = str(depth.mid)
        except Exception:
            log.exception(
                "place_order_mid_fetch_failed", canonical=request.canonical
            )
            mid_str = None

        await record_pending_order(
            self._db,
            client_order_id=cid,
            paper_trade_id=request.paper_trade_id,
            coin_id=request.canonical.lower(),
            symbol=request.canonical,
            venue=self.venue_name,
            pair=request.venue_pair,
            signal_type="",  # filled by engine layer; OK to leave empty here
            size_usd=str(request.size_usd),
            mid_at_entry=mid_str,
        )

        body = await self._signed_post(
            "/api/v3/order",
            params={
                "symbol": request.venue_pair,
                "side": request.side.upper(),
                "type": "MARKET",
                "quoteOrderQty": str(request.size_usd),
                "newClientOrderId": cid,
            },
        )
        venue_order_id = str(body.get("orderId", ""))

        # Update live_trades.entry_order_id for the dedup contract on retries
        await self._db._conn.execute(
            "UPDATE live_trades SET entry_order_id = ? WHERE client_order_id = ?",
            (venue_order_id, cid),
        )
        await self._db._conn.commit()

        log.info(
            "place_order_submitted",
            client_order_id=cid,
            venue_order_id=venue_order_id,
            venue_pair=request.venue_pair,
        )
        return venue_order_id
```

- [ ] **Step 4: Run — expect 2 PASS**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(live-m1.5a): place_order_request runtime body with idempotency (BL-NEW-LIVE-HYBRID M1.5a Task 4)"
```

---

## Task 5: `await_fill_confirmation` runtime body + slippage compute

**Plan-stage 2-reviewer fixes folded (R1-C4, R1-C5, R2-C2):** see AMENDMENTS A5 at end of file. Implementer MUST:
- Resolve `symbol` ONCE via `SELECT pair FROM live_trades WHERE client_order_id = ?` BEFORE entering the poll loop — cache the string + reuse (R1-C4 — original plan's `_symbol_from_cid` returning empty string was a known-incomplete that would have broken every poll)
- Mark `_extract_avg_fill_price` as a SYNC helper (no `async`/`await`) — original plan had async with no await body; calls were missing `await` (R1-C5)
- Treat `PARTIALLY_FILLED` as terminal-with-warning (return immediately with `status='partial'`); document that engine layer is responsible for follow-up reconciliation if Binance later cancels the unfilled remainder (M2 reconciliation worker scope)
- Wrap `_write_slippage_bps` UPDATE in `db._txn_lock`
- Document the `fill_slippage_bps` semantic clearly in docstring + AMENDMENTS A5: it includes ~200-500ms market drift between `place_order_request`'s `fetch_depth` and the actual fill — measures "drift-inclusive slippage proxy", NOT pure venue execution slippage (R2-C2). Column name retained from M1 migration to avoid schema churn. V1 review's approval-removal gate uses median of 30 fills which averages drift to ~0.

**Files:**
- Modify: `scout/live/binance_adapter.py`
- Test: `tests/test_live_binance_adapter_signed.py` (extend)

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_await_fill_confirmation_terminal_filled_writes_slippage(tmp_path):
    from scout.db import Database

    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed a live_trades row in 'open' state with mid_at_entry set
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES ('btc-tok', 'BTC', 'btc', 'ethereum', 'first_signal', '{}',
                   100, 50, 0.5, 120, 80, 'open',
                   '2026-05-09T00:00:00+00:00')"""
    )
    paper_id = (await (await db._conn.execute("SELECT MAX(id) FROM paper_trades")).fetchone())[0]
    cid = "gecko-1-abcd1234"
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, mid_at_entry, status, client_order_id, created_at)
           VALUES (?, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                   '50', '50000.0', 'open', ?, '2026-05-09T00:00:00+00:00')""",
        (paper_id, cid),
    )
    await db._conn.commit()

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    # Simulate Binance returning FILLED on the first poll with a fill price
    # 50 bps above the entry mid (50,000 → 50,250)
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/order",
            payload={
                "orderId": 88888,
                "status": "FILLED",
                "executedQty": "0.001",
                "fills": [{"price": "50250.0", "qty": "0.001"}],
            },
        )
        confirmation = await adapter.await_fill_confirmation(
            venue_order_id="88888",
            client_order_id=cid,
            timeout_sec=2.0,
        )
        assert confirmation.status == "filled"
        assert confirmation.fill_price == pytest.approx(50250.0)
    # Verify slippage_bps was written: (50250 / 50000 - 1) * 10000 = 50.0
    cur = await db._conn.execute(
        "SELECT fill_slippage_bps FROM live_trades WHERE client_order_id = ?",
        (cid,),
    )
    row = await cur.fetchone()
    assert row[0] == pytest.approx(50.0)
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_await_fill_confirmation_timeout_returns_pending(tmp_path):
    """If the order stays 'NEW'/'PARTIALLY_FILLED' through timeout, return
    OrderConfirmation(status='timeout')."""
    from scout.db import Database

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        # Repeat the NEW response indefinitely
        for _ in range(20):
            m.get(
                "https://api.binance.com/api/v3/order",
                payload={"orderId": 88888, "status": "NEW"},
            )
        confirmation = await adapter.await_fill_confirmation(
            venue_order_id="88888",
            client_order_id="gecko-1-deadbeef",
            timeout_sec=0.5,  # short for test
        )
        assert confirmation.status == "timeout"
    await adapter.close()
    await db.close()
```

- [ ] **Step 2: Run — expect 2 FAILs**

- [ ] **Step 3: Replace stub**

```python
    async def await_fill_confirmation(
        self,
        *,
        venue_order_id: str,
        client_order_id: str,
        timeout_sec: float,
    ) -> OrderConfirmation:
        """Poll Binance /api/v3/order until terminal state or timeout.

        Adaptive backoff: 200ms → 500ms → 1s → 2s → 2s ... Reduces
        request volume on slow fills while keeping fast fills snappy.

        Terminal Binance statuses → OrderConfirmation:
          FILLED              → status='filled'
          PARTIALLY_FILLED    → status='partial' (also terminal-ish)
          CANCELED, EXPIRED   → status='rejected'
          REJECTED            → status='rejected'
          NEW, PENDING_CANCEL → keep polling

        On terminal FILLED/PARTIAL: compute fill_slippage_bps using
        live_trades.mid_at_entry vs the avg fill price, write back to
        live_trades.fill_slippage_bps. (Per V1 reviewer C3 closure.)
        """
        backoff_schedule = [0.2, 0.5, 1.0, 2.0]
        deadline = asyncio.get_event_loop().time() + timeout_sec
        attempt = 0
        last_body: dict[str, Any] = {}

        while asyncio.get_event_loop().time() < deadline:
            try:
                body = await self._signed_get(
                    "/api/v3/order",
                    params={
                        "symbol": self._symbol_from_cid(client_order_id),
                        "origClientOrderId": client_order_id,
                    },
                )
                last_body = body
                status_str = body.get("status", "")
                if status_str == "FILLED":
                    fill_price = self._extract_avg_fill_price(body)
                    await self._write_slippage_bps(client_order_id, fill_price)
                    return OrderConfirmation(
                        venue=self.venue_name,
                        venue_order_id=venue_order_id,
                        client_order_id=client_order_id,
                        status="filled",
                        filled_qty=float(body.get("executedQty", "0")),
                        fill_price=fill_price,
                        raw_response=body,
                    )
                if status_str == "PARTIALLY_FILLED":
                    fill_price = self._extract_avg_fill_price(body)
                    return OrderConfirmation(
                        venue=self.venue_name,
                        venue_order_id=venue_order_id,
                        client_order_id=client_order_id,
                        status="partial",
                        filled_qty=float(body.get("executedQty", "0")),
                        fill_price=fill_price,
                        raw_response=body,
                    )
                if status_str in ("CANCELED", "EXPIRED", "REJECTED"):
                    return OrderConfirmation(
                        venue=self.venue_name,
                        venue_order_id=venue_order_id,
                        client_order_id=client_order_id,
                        status="rejected",
                        filled_qty=None,
                        fill_price=None,
                        raw_response=body,
                    )
                # NEW / PENDING_CANCEL — keep polling
                wait = backoff_schedule[min(attempt, len(backoff_schedule) - 1)]
                attempt += 1
                await asyncio.sleep(wait)
            except (BinanceAuthError, VenueTransientError):
                # Auth/transient: surface to caller as timeout w/ raw_response
                # for log inspection. Engine handles by writing a needs_manual_review row.
                break

        return OrderConfirmation(
            venue=self.venue_name,
            venue_order_id=venue_order_id,
            client_order_id=client_order_id,
            status="timeout",
            filled_qty=None,
            fill_price=None,
            raw_response=last_body or None,
        )

    def _symbol_from_cid(self, client_order_id: str) -> str:
        """Look up the venue_pair from live_trades.client_order_id.

        Binance /api/v3/order requires `symbol` even with origClientOrderId.
        """
        if self._db is None:
            return ""
        # Use synchronous attribute access not safe in async context — return
        # cached or rely on adapter setup; for M1.5a, derive from client_order_id
        # not feasible (cid is `gecko-{paper}-{uuid8}`). Caller passes via DB lookup.
        # SIMPLIFICATION: caller writes mid_at_entry + venue_pair into live_trades
        # and we'll do a tiny SELECT here per poll. Acceptable cost for M1.5a;
        # M1.5b can cache.
        return ""  # populated by _signed_get below — see Step 3 NOTE

    async def _extract_avg_fill_price(self, body: dict[str, Any]) -> float:
        """Compute volume-weighted avg fill price from Binance fills array."""
        fills = body.get("fills", []) or []
        total_qty = 0.0
        total_quote = 0.0
        for fill in fills:
            qty = float(fill.get("qty", "0"))
            price = float(fill.get("price", "0"))
            total_qty += qty
            total_quote += qty * price
        if total_qty <= 0:
            return float(body.get("avgPrice", "0") or "0")
        return total_quote / total_qty

    async def _write_slippage_bps(
        self, client_order_id: str, fill_price: float
    ) -> None:
        """Compute (fill_price/mid_at_entry - 1) * 10000 + write to live_trades."""
        if self._db is None or self._db._conn is None:
            return
        cur = await self._db._conn.execute(
            "SELECT mid_at_entry FROM live_trades WHERE client_order_id = ?",
            (client_order_id,),
        )
        row = await cur.fetchone()
        if row is None or row[0] is None:
            return
        mid = float(row[0])
        if mid <= 0:
            return
        slippage_bps = round((fill_price / mid - 1.0) * 10000.0, 2)
        await self._db._conn.execute(
            "UPDATE live_trades SET fill_slippage_bps = ? "
            "WHERE client_order_id = ?",
            (slippage_bps, client_order_id),
        )
        await self._db._conn.commit()
```

**NOTE on `_symbol_from_cid`:** the simplification above is a known incomplete — `_signed_get` for `/api/v3/order` requires `symbol`. Resolve by fetching from `live_trades.pair WHERE client_order_id = ?` once at start of poll loop, NOT per-iteration. Implementer should fix this cleanly.

- [ ] **Step 4: Run — expect 2 PASS** (the symbol lookup is solved properly during impl)

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(live-m1.5a): await_fill_confirmation runtime body + slippage write (BL-NEW-LIVE-HYBRID M1.5a Task 5)"
```

---

## Task 6: Wire balance_gate into Gates Gate 10

**Plan-stage 2-reviewer fixes folded (R1-I3):** move `from scout.live.balance_gate import check_sufficient_balance` to top of `scout/live/gates.py` (matches existing convention at gates.py:28-31). Lazy import inside the gate body is a code smell — there's no circular dependency to dodge (balance_gate doesn't import gates).

**Files:**
- Modify: `scout/live/gates.py`
- Test: `tests/test_live_gates_balance_runtime.py` (NEW)

- [ ] **Step 1: Failing tests**

```python
"""BL-NEW-LIVE-HYBRID M1.5a: Gate 10 balance runtime."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scout.db import Database
from scout.live.gates import Gates


_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


# Tests reuse the existing _make_gates fixture pattern from
# tests/live/test_pretrade_gates.py


@pytest.mark.asyncio
async def test_gate_balance_blocks_when_insufficient_in_live_mode(tmp_path):
    """Gate 10 must call balance_gate.check_sufficient_balance and return
    insufficient_balance reject when balance < required_with_margin."""
    # ...stub adapter returning balance=10, requested=100×1.1=110...
    # ...assert reject_reason='insufficient_balance'...
    pass


@pytest.mark.asyncio
async def test_gate_balance_passes_when_sufficient_in_live_mode(tmp_path):
    # ...stub adapter returning balance=200, requested=100...
    # ...assert passed=True...
    pass


@pytest.mark.asyncio
async def test_gate_balance_skipped_in_shadow_mode(tmp_path):
    """LIVE_MODE='shadow' → Gate 10 is a no-op (no adapter call, no
    NotImplementedError). Existing BL-055 contract."""
    pass
```

- [ ] **Step 2: Run — expect 3 FAILs**

- [ ] **Step 3: Replace Gate 10 NotImplementedError**

In `scout/live/gates.py` Gate 10 block:

```python
        # Gate 10: balance (live-only). Wired in M1.5a — replaces M1's
        # NotImplementedError stub. Calls balance_gate.check_sufficient_
        # balance which queries adapter.fetch_account_balance.
        if self._config.mode == "live":
            from scout.live.balance_gate import check_sufficient_balance

            result = await check_sufficient_balance(
                self._adapter,
                float(size_usd),
                margin_factor=1.1,
            )
            if not result.passed:
                return (
                    GateResult(
                        passed=False,
                        reject_reason="insufficient_balance",
                        detail=result.detail,
                    ),
                    venue,
                )
```

- [ ] **Step 4: Run — expect 3 PASS**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(live-m1.5a): Gate 10 balance runtime (replaces M1 NotImplementedError) — BL-NEW-LIVE-HYBRID M1.5a Task 6"
```

---

## Task 7: main.py startup — Layer 1 + balance smoke check

**Plan-stage 2-reviewer fixes folded (R1-I4, R2-I1, R2-I5):** see AMENDMENTS A7. Implementer MUST:
- Drop redundant `except (asyncio.TimeoutError, Exception)` — `Exception` already catches `TimeoutError`. Use single `except Exception as exc:` after the explicit `except BinanceAuthError` branch (R1-I4)
- Add operator-facing message to Step 7 post-deploy smoke instructions: "**M1.5a smoke pass ≠ live ready.** Approval-gate runtime call (V1-C1) + correction-counter increment on close (V1-C2) are M1.5b. Do NOT flip `live_eligible=1` for any signal until M1.5b ships." (R2-I1)
- Apply Telegram startup notification rate-limit (R2-I5): query `paper_migrations` table for last_startup_notification timestamp; skip the alert if `< LIVE_STARTUP_NOTIFICATION_MIN_INTERVAL_SEC` ago. Use `paper_migrations` since it already exists; key name `live_startup_notification_last_sent`. Default 300s (5 min) prevents Telegram spam during systemd restart loop (R2-C1 mitigation)

**Files:**
- Modify: `scout/main.py`
- Test: `tests/test_live_main_startup_balance_smoke.py` (NEW)

- [ ] **Step 1: Failing tests**

```python
"""BL-NEW-LIVE-HYBRID M1.5a: main.py LIVE_MODE='live' startup guards."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


@pytest.mark.asyncio
async def test_live_mode_live_without_master_kill_raises_runtime():
    """LIVE_MODE=live + LIVE_TRADING_ENABLED=False → RuntimeError("Layer 1")"""
    pass


@pytest.mark.asyncio
async def test_live_mode_live_with_master_kill_proceeds_to_balance_smoke():
    """LIVE_MODE=live + LIVE_TRADING_ENABLED=True → adapter.fetch_account_balance
    is called once at boot."""
    pass


@pytest.mark.asyncio
async def test_live_mode_live_balance_smoke_failure_aborts_startup():
    """fetch_account_balance raises BinanceAuthError → main raises with
    operator-facing message naming the auth failure."""
    pass
```

- [ ] **Step 2: Run — expect 3 FAILs**

- [ ] **Step 3: Replace startup guard in scout/main.py**

In the `if live_config.mode == "live":` block, replace the existing NotImplementedError:

```python
    if live_config.mode in ("shadow", "live"):
        if live_config.mode == "live":
            # BL-NEW-LIVE-HYBRID M1.5a — Layer 1 master-kill guard
            if not getattr(settings, "LIVE_TRADING_ENABLED", False):
                await db.close()
                raise RuntimeError(
                    "LIVE_MODE=live requires LIVE_TRADING_ENABLED=True "
                    "(Layer 1 master kill). Operator must set "
                    "LIVE_TRADING_ENABLED=True in .env."
                )
            if not settings.BINANCE_API_KEY or not settings.BINANCE_API_SECRET:
                await db.close()
                raise RuntimeError("LIVE_MODE=live requires BINANCE_API_KEY/SECRET")
            # M1.5a — balance smoke check (replaces M1's NotImplementedError).
            # Construct a temporary adapter, fetch USDT balance once with
            # 5s timeout. If it raises, refuse to start with the
            # operator-facing failure mode named.
            from scout.live.binance_adapter import (
                BinanceAuthError,
                BinanceSpotAdapter,
            )

            smoke_adapter = BinanceSpotAdapter(settings, db=db)
            try:
                await asyncio.wait_for(
                    smoke_adapter.fetch_account_balance("USDT"),
                    timeout=5.0,
                )
            except BinanceAuthError as exc:
                await smoke_adapter.close()
                await db.close()
                raise RuntimeError(
                    f"LIVE_MODE=live balance smoke check failed: {exc}. "
                    "Verify BINANCE_API_KEY/SECRET + IP whitelist."
                ) from exc
            except (asyncio.TimeoutError, Exception) as exc:
                await smoke_adapter.close()
                await db.close()
                raise RuntimeError(
                    f"LIVE_MODE=live balance smoke check failed: {type(exc).__name__}: {exc}"
                ) from exc
            await smoke_adapter.close()
```

- [ ] **Step 4: Run — expect 3 PASS**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(live-m1.5a): main.py startup balance smoke check + Layer 1 guard (BL-NEW-LIVE-HYBRID M1.5a Task 7)"
```

---

## Task 7.5: systemd unit hardening — RestartSec + StartLimitBurst (R2-C1)

**Why this task exists:** R2-C1 plan-stage finding caught that smoke-check failure in main.py raises `RuntimeError` → systemd default `Restart=on-failure` + no backoff = sub-second restart loop hitting Binance auth at 50+ req/s within minutes → IP-ban risk. M1.5a deploy MUST land alongside systemd unit hardening.

**Files:**
- VPS-side: `/etc/systemd/system/gecko-pipeline.service` (operator-edited)
- Repo-side: documentation in `docs/runbooks/live-trading-deploy.md` (NEW or extend existing)

- [ ] **Step 1: Inspect current unit file**

```bash
ssh root@89.167.116.187 'systemctl cat gecko-pipeline.service' > .ssh_systemd_current.txt 2>&1
```

Read the output. Look for `[Service]` block fields: `Restart`, `RestartSec`, `StartLimitBurst`, `StartLimitIntervalSec`.

- [ ] **Step 2: Patch unit file**

Required `[Service]` block fields:

```ini
Restart=on-failure
RestartSec=30s
StartLimitBurst=3
StartLimitIntervalSec=300s
```

This means: on failure, wait 30s before restart. Allow at most 3 restart attempts in 300s (5 min). Beyond that, systemd marks the service `failed` and stops trying — operator must manually intervene with `systemctl reset-failed gecko-pipeline && systemctl start gecko-pipeline` after fixing root cause.

```bash
# On VPS (operator-side):
sudo systemctl edit gecko-pipeline.service
# Add the 4 fields under [Service]
sudo systemctl daemon-reload
sudo systemctl restart gecko-pipeline
```

- [ ] **Step 3: Verify**

```bash
ssh root@89.167.116.187 'systemctl show gecko-pipeline.service | grep -E "Restart=|RestartSec=|StartLimitBurst=|StartLimitIntervalSec="' > .ssh_systemd_verify.txt 2>&1
```

Expected output:
```
Restart=on-failure
RestartSec=30000000
StartLimitBurst=3
StartLimitIntervalUSec=5min
```

- [ ] **Step 4: Document in runbook**

Add to `docs/runbooks/live-trading-deploy.md` (create if missing):

```markdown
## systemd unit requirements for LIVE_MODE='live'

Before flipping LIVE_MODE='live', the gecko-pipeline.service unit
MUST have:

- Restart=on-failure
- RestartSec=30s         # 30s backoff prevents Binance IP-ban
- StartLimitBurst=3      # max 3 restart attempts
- StartLimitIntervalSec=300s  # before systemd gives up

If the smoke check fails 3 times in 5 min, systemd marks the service
'failed' and stops trying. Operator wakes up to a dead pipeline (not a
50-req/s loop hitting Binance auth) and must:
  1. Investigate failure mode (auth / network / IP whitelist)
  2. Fix root cause
  3. systemctl reset-failed gecko-pipeline
  4. systemctl start gecko-pipeline
```

- [ ] **Step 5: Commit doc + add to deploy checklist**

```bash
git add docs/runbooks/live-trading-deploy.md
git commit -m "docs(live-m1.5a): systemd hardening runbook (RestartSec + StartLimitBurst) — Task 7.5"
```

---

## Task 8: BinanceAuthError export + small cleanups

**Files:**
- Modify: `scout/live/binance_adapter.py`
- Modify: `scout/live/exceptions.py` (move BinanceAuthError there if test imports from `scout.live.binance_adapter` need it elsewhere; defer if unnecessary)

- [ ] **Step 1: Verify imports still work**

- [ ] **Step 2: Check test imports**

```bash
grep -rn "BinanceAuthError" tests/ scout/ 2>&1 | head -10
```

- [ ] **Step 3: Commit any cleanup**

---

## Task 9: Full regression + black + PR + 3-vector reviewers + merge

- [ ] **Step 1: Full M1 + M1.5a regression**

```bash
uv run --native-tls pytest tests/test_live_*.py tests/live/ tests/integration/test_live_shadow_loop.py -q
```

All M1 tests + new M1.5a tests + BL-055 tests pass.

- [ ] **Step 2: Black**

```bash
uv run black scout/ tests/
```

- [ ] **Step 3: Open PR + dispatch 3-vector reviewers**

Per CLAUDE.md §8 attack-vector orthogonality (this PR touches money flows directly):
- **Vector 1 — Statistical / Policy / Slippage / Auth-error mapping**: are auth/transient errors mapped correctly? Does slippage compute round-trip cleanly? Are HMAC fixtures locked in test?
- **Vector 2 — Structural / Code / Idempotency contract**: is the dedup pre-check race-safe? Does the live_trades.client_order_id UNIQUE constraint backstop properly? Is balance_gate composition with Gate 10 clean?
- **Vector 3 — Strategy / Blast-radius / Reversibility / Operator-action**: what happens on first prod boot with malformed creds? On Binance maintenance window? Is the smoke check cleanly reversible (operator can flip LIVE_MODE='live' and back without DB damage)?

- [ ] **Step 4: Apply MUST-FIX findings + commit**

- [ ] **Step 5: Mark ready + squash-merge + delete-branch**

- [ ] **Step 6: Deploy to VPS** (LIVE_MODE stays 'paper'/'shadow' — no behavior change unless operator flips)

```bash
ssh root@89.167.116.187 'systemctl stop gecko-pipeline && cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} +; systemctl start gecko-pipeline && sleep 5 && systemctl is-active gecko-pipeline' > .ssh_deploy_m1_5a.txt 2>&1
```

- [ ] **Step 7: Post-deploy operator-side smoke instructions**

After the deploy, the operator can run a one-shot live-mode smoke test in a TEMPORARY .env:
```bash
# In a temp .env, set:
#   LIVE_MODE=live
#   LIVE_TRADING_ENABLED=True
#   BINANCE_API_KEY=<testnet key>
#   BINANCE_API_SECRET=<testnet secret>
# Then start the pipeline. Expected: process boots, smoke check passes,
# pipeline runs cycles (LIVE_MODE='live' no longer hard-blocks).
# Revert .env after smoke test — M1.5b wires the actual signal-driven
# trade execution path; M1.5a only verifies the auth + balance plumbing.
```

- [ ] **Step 8: Memory + todo update**

Write `project_live_m1_5a_shipped_<DATE>.md`. Update `tasks/todo.md` with M1.5a status + soak window for the next deferred work item (M1.5b engine wiring).

---

## Done criteria

- All new tests pass; full regression clean; black clean
- 0 schema migrations introduced
- 4 NotImplementedError stubs in BinanceSpotAdapter replaced (3 ABC + Gate 10 was 2x too pessimistic — M1 had `place_order_request`, `await_fill_confirmation`, `fetch_account_balance` all stubs; M1.5a replaces all 3)
- 1 NotImplementedError in scout/main.py replaced (balance_gate not wired)
- LIVE_MODE='live' boots cleanly under correct config (no NotImplementedError)
- LIVE_MODE='live' refuses to boot under bad config with operator-facing messages (Layer 1 missing OR BINANCE_API_KEY missing OR balance smoke fails)
- Operator can run testnet smoke test post-deploy
- M1.5b plan can be drafted (engine routing dispatch + approval gateway wiring + correction counter) without architectural rework

## What this milestone does NOT do

- Does NOT call `RoutingLayer.get_candidates` from engine (M1.5b)
- Does NOT call `should_require_approval` from engine (M1.5b)
- Does NOT increment `signal_venue_correction_count.consecutive_no_correction` on close (M1.5b)
- Does NOT add operator notifications post-fill (M1.5b)
- Does NOT wire CCXTAdapter to any venue (M2)

## V2 deferred items (still deferred — non-blocking for M1.5a)

V2 review's I1-I4 + hidden seam still apply post-M1.5a:
- I1: ServiceRunner cancel-log gap
- I2: Lock-contract test gap
- I3: cross_venue_exposure CAST asymmetry
- I4: `_apply_override_prepend` venue NULL handling
- Hidden seam: no staleness gate on `venue_health.probe_at`

Bundle into M1.5b PR or separate cleanup PR.

---

## AMENDMENTS — plan-stage 2-reviewer findings folded 2026-05-09

R1 (structural/code) + R2 (strategy/blast-radius) returned with 6 R1 critical + 2 R2 critical + 9 important + 4 minor. Itemized resolution:

### R1 Critical

| ID | Finding | Resolution location |
|---|---|---|
| R1-C1 | `_signed_get` duplicated `_http_get` retry loop | NEW Task 1.5 (refactor `_request` core); Task 2 Step 3 rewrites helpers as thin wrappers |
| R1-C2 | `_signed_post` skipped retry entirely | Task 1.5 `_request` covers POST 5xx retry path; Task 2 Step 3.5 adds explicit test |
| R1-C3 | Idempotency race: concurrent INSERT not caught | Task 4 inline pointer A4 — `try/except sqlite3.IntegrityError` + re-call lookup |
| R1-C4 | `_symbol_from_cid` returned empty string (broken) | Task 5 inline pointer A5 — pre-loop `SELECT pair FROM live_trades WHERE client_order_id = ?` cached |
| R1-C5 | `_extract_avg_fill_price` async-no-await mismatch | Task 5 inline pointer A5 — sync helper, drop `async` |
| R1-C6 | Empty-string `orderId` fallback poisons dedup | Task 4 inline pointer A4 — raise `VenueTransientError` if missing |

### R2 Critical

| ID | Finding | Resolution location |
|---|---|---|
| R2-C1 | Smoke-check fail → infinite systemd restart loop | NEW Task 7.5 (systemd unit hardening: `RestartSec=30s`, `StartLimitBurst=3`, runbook) |
| R2-C2 | `fill_slippage_bps` semantic conflates drift + execution | Task 5 inline pointer A5 — column name retained, docstring + plan §0 clarify "drift-inclusive proxy"; V1 review's median-of-30 averages drift to ~0 |

### R1 Important

| ID | Finding | Resolution location |
|---|---|---|
| R1-I1 | 418 IP-ban not handled | Task 1.5 `_request` raises `BinanceIPBanError` on 418 |
| R1-I2 | `db._txn_lock` not acquired around UPDATE | Task 4 + Task 5 inline pointers — wrap UPDATEs in `async with self._db._txn_lock:` |
| R1-I3 | Gate 10 lazy import code smell | Task 6 inline pointer — top-of-module `from scout.live.balance_gate import` |
| R1-I4 | Redundant `except (asyncio.TimeoutError, Exception)` | Task 7 inline pointer A7 — single `except Exception` after explicit `BinanceAuthError` |
| R1-I5 | Tests-on-Windows OpenSSL gotcha | Task 2 Step 3.5 note — implementer adds source-text-inspection or stub-adapter parallel tests for cross-platform; aiohttp tests OK on CI Linux |
| R1-I6 | Test fixture leakage | Implementer reuses `tests/conftest.py:60 settings_factory` — no inline duplication |

### R2 Important

| ID | Finding | Resolution location |
|---|---|---|
| R2-I1 | Operator might think smoke-pass = live-ready | Task 7 inline pointer A7 — explicit "smoke pass ≠ live ready; M1.5b wires V1-C1+C2" message |
| R2-I2 | `_signed_post` doesn't handle -2010 (duplicate cid) | Task 1.5 `_request` raises `BinanceDuplicateOrderError`; Task 4 catches it, recovers via `origClientOrderId` lookup |
| R2-I3 | Weight governor — signed endpoints heavier (M1.5b consideration) | Acknowledged as M1.5b scope; not blocking M1.5a |
| R2-I4 | No fast revert path | NEW Settings field `LIVE_USE_REAL_SIGNED_REQUESTS: bool = False` (Task 0 Step 4); when False, runtime bodies fall back to NotImplementedError |
| R2-I5 | Telegram startup notification spam under restart loop | NEW Settings field `LIVE_STARTUP_NOTIFICATION_MIN_INTERVAL_SEC: int = 300` (Task 0 Step 4); Task 7 inline pointer A7 — query `paper_migrations` for last_sent timestamp |

### R1 Minor

| ID | Finding | Resolution location |
|---|---|---|
| R1-M1 | Done-criteria off-by-one count | Task 9 done-criteria edited inline (3 stubs not 4) |
| R1-M2 | Slippage write race documented but not gated | Task 5 inline — txn_lock acquired (R1-I2 covers) |
| R1-M3 | `--native-tls` flag in test commands | Documented as Windows TLS workaround; CI Linux runs without it. Acceptable plan-document divergence. |
| R1-M4 | Step 0 prereq was "flag and continue" | Task 0 Step 3 edited inline — hardened to STOP / report BLOCKED |

### Test matrix expansion (R1 + R2)

Implementer adds these tests beyond the original plan's set:

- `test_signed_get_raises_on_timestamp_drift` — -1021 path (Task 2 Step 3.5)
- `test_signed_endpoint_raises_ip_ban_on_418` — 418 distinct from 429 (Task 2 Step 3.5)
- `test_signed_post_retries_on_5xx` — POST 5xx tolerance (Task 2 Step 3.5)
- `test_place_order_request_handles_integrity_error_race` — concurrent INSERT collision (Task 4 extension)
- `test_place_order_request_handles_duplicate_order_2010` — Binance dedup recovery (Task 4 extension)
- `test_place_order_request_rejects_empty_order_id` — orderId fallback safety (Task 4 extension)
- `test_await_fill_confirmation_resolves_symbol_from_db_once` — pre-loop SELECT cached (Task 5 extension)
- `test_await_fill_confirmation_skips_slippage_write_when_mid_null` — graceful skip (Task 5 extension)
- `test_smoke_check_failure_does_not_create_runtime_dependency_on_db` — main.py recovery (Task 7 extension)

### Net plan delta

- +2 new tasks (1.5 + 7.5)
- +2 new Settings fields (`LIVE_USE_REAL_SIGNED_REQUESTS`, `LIVE_STARTUP_NOTIFICATION_MIN_INTERVAL_SEC`)
- +9 test cases beyond original plan
- +1 documentation file (`docs/runbooks/live-trading-deploy.md`)
- +1 dependency on systemd unit hardening (operator-side)
- 0 new schema migrations (M1.5a still migration-free)

Net plan size: ~50 → ~70 implementation steps. Same 9-task structure remains the primary frame; Task 1.5 + 7.5 are additions.
