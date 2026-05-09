**New primitives introduced:** Architectural document complementing `tasks/plan_live_trading_milestone_1_5a.md` — describes Binance REST signing seam, idempotency contract under concurrency, smoke-check operator-action sequencing, reversibility model. No new primitives beyond what the plan introduces.

# M1.5a Design — Binance REST Signing + Runtime Bodies

**Status:** spec-as-of-2026-05-09 | **Plan:** `tasks/plan_live_trading_milestone_1_5a.md` (commit `94584ee`) | **PR target:** `feat/live-trading-m1-5a`

## 1. Goal + non-goals

**Goal:** unblock `LIVE_MODE='live'` boot and prepare the chokepoint for M1.5b's signal-driven execution path. Replace the 4 NotImplementedError stubs landed in M1 (3 ABC methods + Gate 10 + main.py balance-gate guard) with real Binance REST signing.

**Non-goals (deferred to M1.5b):**
- Engine-side wiring of `RoutingLayer.get_candidates`
- Engine-side `should_require_approval` call
- `signal_venue_correction_count.consecutive_no_correction` increment on close events
- Operator post-fill Telegram notifications
- CCXTAdapter wired to a real venue

## 2. Architecture decisions

### 2.1 Native HMAC-SHA256, NOT CCXT

Per `tasks/findings_ccxt_verification_2026_05_08.md`, M1's architectural commitment was: **keep BL-055 native for Binance, use CCXT only for the long tail.** M1.5a honors this.

CCXT's REST surface is solid; only WS reliability has gaps. So M1.5a *could* use `ccxt.async_support.binance` and inherit signing for free (~5 LOC). But:

1. The pinned ccxt 4.5.52 dependency is heavy (12 transitive deps including cryptography + requests + urllib3). Native HMAC needs only stdlib.
2. CCXT version pinning policy (per the verification doc): "operator-driven quarterly bumps." A native signing primitive is exempt from that ceremony.
3. The native primitive is ~50 LOC + tested against Binance's published HMAC fixture — fully auditable, no transitive surface.

Trade-off: native means we re-implement what CCXT already has. M1.5a chose audit-and-control over leverage. M2 may revisit if the long-tail path expands enough that CCXT becomes the dominant code path.

### 2.2 `_request(method, path, *, params, headers, signed)` core

Plan-stage R1 review caught that the original M1.5a plan duplicated `_http_get`'s retry/weight/429 handling inside `_signed_get`+`_signed_post`. Drift between the two retry paths is guaranteed.

**Fix (Task 1.5):** extract `_request` core. Both unsigned (`_http_get`) and signed (`_signed_get`/`_signed_post`) wrappers go through it. Signed callers pre-inject signature; the core handles HTTP semantics + Binance-specific error taxonomy.

```
        ┌──────────────────────────┐
        │ caller (e.g. fetch_acct) │
        └──────────────┬───────────┘
                       │
              ┌────────▼────────┐
              │ _signed_get     │  ← injects timestamp, recvWindow, sig, X-MBX-APIKEY
              │ _signed_post    │
              │ _http_get       │  ← passes through unchanged
              └────────┬────────┘
                       │
              ┌────────▼────────┐
              │ _request(core)  │  ← retry, weight gov, 429, 5xx, 418, auth-error map
              └─────────────────┘
```

Code shape: 1 retry loop, 1 weight call, 1 set of error mappings. Adding a new error code (e.g. -2010 duplicate clientOrderId) is a single edit.

### 2.3 Idempotency contract under concurrency

Plan-stage R1-C3 + R2-I2: two failure modes for `place_order_request`'s dedup contract.

**Failure mode A (race on INSERT):** concurrent retries both pass `lookup_existing_order_id` (returns None) → both call `record_pending_order` → second hits `IntegrityError` on UNIQUE INDEX `idx_live_trades_client_order_id` (M1 Task 12 schema 20260509).

**Failure mode B (race on Binance):** first retry posted the order successfully but never received the response (network drop, container kill). Second retry passes the dedup check (no row exists yet because first retry crashed before `record_pending_order`), submits to Binance, gets `-2010 duplicate newClientOrderId` because Binance still has the first one.

**Resolution:**

```
place_order_request(request):
  cid = make_client_order_id(request.paper_id, request.intent_uuid)

  # Step 1: cheap dedup
  existing_order_id = lookup_existing_order_id(db, cid)
  if existing_order_id is not None:
    return existing_order_id

  # Step 2: capture mid_at_entry
  depth = fetch_depth(request.venue_pair)

  # Step 3: insert pending row, handle race
  try:
    record_pending_order(db, cid=cid, ...)  # acquires _txn_lock
  except IntegrityError:
    # Failure mode A — another retry beat us
    existing_order_id = lookup_existing_order_id(db, cid)
    if existing_order_id is not None:
      return existing_order_id
    # Race could leave entry_order_id NULL if the winning retry crashed
    # post-INSERT. Fall through to submit; -2010 below catches.

  # Step 4: submit to Binance
  try:
    body = await _signed_post("/api/v3/order", params={..., newClientOrderId=cid})
  except BinanceDuplicateOrderError:  # -2010
    # Failure mode B — Binance already has this order from a prior retry
    body = await _signed_get("/api/v3/order", params={origClientOrderId=cid})
    # Recovery: read existing order, persist orderId, return.

  # Step 5: persist + return
  if not body.get("orderId"):
    raise VenueTransientError(...)  # never persist empty orderId
  async with db._txn_lock:
    db.execute("UPDATE live_trades SET entry_order_id=? WHERE client_order_id=?", ...)
  return str(body["orderId"])
```

This handles both race modes with explicit code paths, no silent retries, and the UNIQUE INDEX is the safety net of last resort (rejected before any duplicate Binance submit).

### 2.4 Smoke check + Layer 1 master kill at startup

Plan-stage R2-C1 + R2-I1 caught two operator-action issues:

**Issue 1**: smoke-check failure under default systemd config = sub-second restart loop hitting Binance auth at 50+ req/s → IP-ban within minutes.

**Issue 2**: operator might read post-deploy "smoke pass = live ready" — actually M1.5b is required for engine wiring.

**Resolution:**

```
main.py startup, when LIVE_MODE='live':

  if not LIVE_TRADING_ENABLED:
    raise RuntimeError("Layer 1 master kill")     ← fail-fast, cheap

  if not BINANCE_API_KEY/SECRET:
    raise RuntimeError("creds missing")            ← fail-fast

  smoke_adapter = BinanceSpotAdapter(settings, db=db)
  try:
    await asyncio.wait_for(
      smoke_adapter.fetch_account_balance("USDT"),
      timeout=5.0,                                 ← bounded
    )
  except BinanceAuthError as exc:
    raise RuntimeError(f"smoke check failed: {exc}; verify creds + IP whitelist")
  except Exception as exc:                         ← single clause, R1-I4 fix
    raise RuntimeError(f"smoke check failed: {type(exc).__name__}: {exc}")
  finally:
    await smoke_adapter.close()
```

Combined with **Task 7.5's systemd hardening** (RestartSec=30s, StartLimitBurst=3), failure mode becomes:

```
boot → smoke fails → systemd waits 30s → boot → smoke fails → ... 
  → after 3 attempts in 5min → systemd marks failed, stops trying
  → operator wakes up to dead pipeline, fixes root cause, manually restarts
```

NOT: 50 req/s for 24h, IP-banned by Binance, alarm fatigue from Telegram restart spam.

### 2.5 Reversibility — `LIVE_USE_REAL_SIGNED_REQUESTS` flag

Plan-stage R2-I4: the 4 NotImplementedError replacements are NOT additive. Reverting M1.5a means `git revert <squash>` which restores the stubs. If revert happens AFTER an operator has flipped `LIVE_MODE='live'`, restored NotImplementedError crashes the next cycle.

**Fix:** Settings field `LIVE_USE_REAL_SIGNED_REQUESTS: bool = False` gates the runtime-body codepath. When False, the 3 ABC methods raise NotImplementedError as before. Operator's emergency revert is a 2-second `.env` flip + restart, not a `git revert`.

```
fetch_account_balance(asset):
  if not self._settings.LIVE_USE_REAL_SIGNED_REQUESTS:
    raise NotImplementedError("LIVE_USE_REAL_SIGNED_REQUESTS=False — emergency revert active")
  # ... real implementation ...
```

Tradeoff: adds 5 LOC × 3 = 15 LOC of feature-flag plumbing. Saves a real production-ops emergency. Operator-action documentation: "if live trading produces unexpected behavior, set LIVE_USE_REAL_SIGNED_REQUESTS=False in .env, systemctl restart. Then triage."

## 3. Data flow — signal to fill

This is the path M1.5a exercises end-to-end (under operator-side LIVE_MODE='live' opt-in):

```
PaperTrader chokepoint (signal fired)
    │
    ▼
LiveEngine.on_paper_trade_opened(paper_trade)  [scout/live/engine.py]
    │
    ▼
Gates.evaluate(signal_type, symbol, size_usd)  [scout/live/gates.py]
    │
    ├── Gate 1-9 (existing M1) ──→ if rejected: DB row + return
    │
    ├── Gate 10: balance check                  [NEW M1.5a]
    │     │
    │     ▼
    │   balance_gate.check_sufficient_balance(adapter, size, margin=1.1)
    │     │
    │     ▼
    │   adapter.fetch_account_balance("USDT")   [NEW M1.5a runtime body]
    │     │
    │     ▼
    │   _signed_get("/api/v3/account") ──→ _request("GET", signed=True)
    │     │
    │     ▼
    │   parse balances[].free → return float
    │
    └── (M1.5b will add: routing dispatch + approval gateway here)

If Gates pass, M1.5b will then call:
    │
    ▼
adapter.place_order_request(OrderRequest(...))  [NEW M1.5a runtime body]
    │
    ├── lookup_existing_order_id(cid)           [M1 idempotency.py]
    ├── fetch_depth(pair) → mid_at_entry        [existing M1]
    ├── record_pending_order(...)               [M1 idempotency.py + R1-C3 race handler]
    ├── _signed_post("/api/v3/order", ...) ──→ _request("POST", signed=True)
    │     ├── BinanceDuplicateOrderError on -2010 ──→ _signed_get to recover
    │     └── on success: orderId in body
    └── UPDATE live_trades SET entry_order_id   [acquires _txn_lock per R1-I2]

Then engine calls (M1.5b):
    │
    ▼
adapter.await_fill_confirmation(venue_order_id, cid, timeout)  [NEW M1.5a runtime body]
    │
    ├── pre-loop: SELECT pair FROM live_trades WHERE cid=?      [R1-C4 fix, cached]
    │
    └── poll loop (200ms → 2s adaptive backoff):
          _signed_get("/api/v3/order", origClientOrderId=cid)
          if status == FILLED:
            extract avg_fill_price (sync helper, R1-C5 fix)
            UPDATE live_trades SET fill_slippage_bps  [acquires _txn_lock]
            return OrderConfirmation(status='filled', ...)
          if status == PARTIALLY_FILLED:
            return OrderConfirmation(status='partial', ...)
          if status in (CANCELED, EXPIRED, REJECTED):
            return OrderConfirmation(status='rejected', ...)
          # else: NEW or PENDING_CANCEL → keep polling
        on timeout: return OrderConfirmation(status='timeout', ...)
```

## 4. Failure mode taxonomy

| Failure | Source | Detection | Action |
|---|---|---|---|
| Bad API key | -2015 in body | `BinanceAuthError` in `_request` | Surface to caller; do NOT retry |
| Bad signature | -2014 in body | `BinanceAuthError` | Surface; check secret correctness |
| Clock skew | -1021 in body | `BinanceAuthError` | Surface; advise NTP sync |
| Duplicate order | -2010 in body | `BinanceDuplicateOrderError` | Caller recovers via origClientOrderId GET |
| IP banned | HTTP 418 | `BinanceIPBanError` | Surface; back off MINUTES (operator action) |
| Rate limited | HTTP 429 | Retry with Retry-After | Inside `_request` retry loop |
| Server error | HTTP 5xx | Retry with backoff | Inside `_request` retry loop, 3 attempts |
| Network drop | aiohttp.ClientError | Retry | Inside `_request` retry loop |
| Order placed but no response | network drop AFTER POST | Caller sees raw `VenueTransientError` | M1.5a: caller raises; engine writes needs_manual_review row in M1.5b |
| Symbol not listed | -1121 sentinel | Returns `{"__code": -1121}` | Caller (e.g. `fetch_exchange_info_row`) translates to None |
| INSERT race | sqlite3.IntegrityError | `try/except` in `place_order_request` | Re-call `lookup_existing_order_id` |
| Empty orderId | body.get("orderId") falsy | Validation in `place_order_request` | `VenueTransientError`, never persist `""` |

## 5. Reversibility — operator runbook

**To revert M1.5a behavior without git:**
```bash
# In .env on VPS:
LIVE_USE_REAL_SIGNED_REQUESTS=False
# systemctl restart gecko-pipeline
```

**To revert via git (heavier):**
```bash
# Local:
git revert <squash-merge-of-PR>
git push
# VPS:
ssh root@VPS 'cd /root/gecko-alpha && git pull && systemctl restart gecko-pipeline'
```

**Pre-revert checklist:**
1. Set `LIVE_MODE='paper'` BEFORE git revert (otherwise restored NotImplementedError will crash next cycle on Gate 10)
2. Verify systemctl status post-restart
3. Watch journal for 60s

## 6. Operator-side prerequisites (gating LIVE_MODE='live' flip post-deploy)

These are NOT in M1.5a's PR scope but are documented for the activation runbook:

1. **Answer 4 design open questions** in `tasks/design_live_trading_hybrid.md` §"Open questions"
2. **Fund Binance account** (start with testnet; production funding only after testnet smoke pass)
3. **Whitelist VPS IP** in Binance API console (89.167.116.187)
4. **Provision testnet API keys** with TRADE permission scope
5. **Set the 4 .env knobs**:
   - `LIVE_TRADING_ENABLED=True` (Layer 1)
   - `LIVE_MODE=live` (Layer 2)
   - `LIVE_USE_REAL_SIGNED_REQUESTS=True` (M1.5a feature flag)
   - `BINANCE_API_KEY` / `BINANCE_API_SECRET`
6. **`UPDATE signal_params SET live_eligible=1` for first signal** (Layer 3)
7. **Verify systemd unit hardened** (Task 7.5: RestartSec=30s, StartLimitBurst=3)
8. **NOTE**: M1.5b is required before signals will actually fire live trades. M1.5a unlocks BOOT, not EXECUTION. Engine still needs routing dispatch + approval gateway wiring.

## 7. Test strategy

**Layer 1 — pure-function tests** (no aiohttp):
- `tests/test_live_binance_signing.py` — HMAC fixture from Binance docs (`c8db568...bd6b71`); locks the contract.

**Layer 2 — adapter unit tests with aioresponses**:
- `tests/test_live_binance_adapter_signed.py` — mocks Binance REST for all auth-error cases (-2014/-2015/-1021/-2010), HTTP 418 (IP ban), 429 (rate limit), 5xx (transient), and success paths.
- Windows note: aiohttp transitively triggers OpenSSL Applink crash on local Windows pytest. Tests run cleanly on CI Linux. For local-Windows coverage, source-text inspection or stub-adapter pattern (see `tests/test_live_balance_gate.py:13-26` for the existing pattern).

**Layer 3 — integration with DB**:
- `tests/test_live_idempotency.py` extension — concurrent INSERT race test (UNIQUE INDEX backstop).
- `tests/test_live_gates_balance_runtime.py` — Gate 10 with stub adapters.

**Layer 4 — startup smoke**:
- `tests/test_live_main_startup_balance_smoke.py` — Layer 1 guard, smoke-check failure modes, Telegram rate-limit.

**Out of scope:** real-network testnet smoke. Operator runs that manually post-deploy per the runbook.

## 8. Open questions (deferred to operator)

1. **Spot vs Futures**: M1.5a targets `https://api.binance.com` (spot) per existing `_BASE_URL`. If operator prefers Binance USDⓈ-M Futures (`fapi.binance.com`), separate adapter + symbol-form changes. Confirm spot is the M1.5a/M1.5b target.

2. **Region**: `api.binance.com` is global. `binance.us` for US operators; `api.binance.je` for Jersey. Not relevant if operator is non-US (per project profile, looks non-US).

3. **`recvWindow` value**: M1.5a hardcodes 5000ms. Binance allows up to 60000. 5000 is conservative (catches clock skew at NTP-level precision); 10000 is common for VPS deploys. Acceptable as default; operator can extend via Settings if -1021 errors recur.

## 9. Approval-removal metric semantic (R2-C2 clarification)

`fill_slippage_bps` semantic:
```
fill_slippage_bps = (fill_price / mid_at_entry - 1) * 10000
```
where `mid_at_entry` is sampled by `place_order_request` via `fetch_depth` BEFORE the order submit.

This includes:
- True venue execution slippage (slippage walking the order book)
- Market drift in the ~200-500ms between `fetch_depth` and the fill arriving

Drift component is symmetric (long-side fills could drift up OR down) and averages to ~0 across the V1 approval-removal gate's median-of-30-fills lookback. So the metric is fit-for-purpose: high-noise on a single fill, low-noise on the rolling cohort the gate actually consumes.

Column name `fill_slippage_bps` retained from M1's migration `bl_live_trades_telemetry_v1` to avoid schema churn. M1.5b *could* split into `fill_drift_bps` + `fill_venue_slippage_bps` if precision becomes load-bearing, but for V1's gate signature the current single column is sufficient.

## 10. What "ready for design review" means

- All 8 plan-stage critical findings folded (R1-C1 through R1-C6 + R2-C1, R2-C2)
- All 11 plan-stage important findings folded
- Net plan delta documented (2 new tasks, 2 new Settings, 9 new test cases, 1 runbook)
- Operator-action sequencing explicit (smoke pass ≠ live ready)
- Failure-mode taxonomy complete
- Data flow diagram aligns with plan
- Reversibility model documented (LIVE_USE_REAL_SIGNED_REQUESTS flag + git revert pre-checklist)

Design review checks: does this architecture get to a "ready to soak shadow → ready to flip live" state without further architecture rework? Are the seams between M1.5a and M1.5b clean enough that M1.5b becomes a 4-5 task PR (engine-side wiring only, no adapter changes)?

If yes: ship to plan-stage build. If no: design-stage 2-reviewer pass surfaces the missing seam.
