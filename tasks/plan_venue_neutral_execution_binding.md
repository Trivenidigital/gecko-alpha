# Venue-neutral execution binding — wiring TradeIntent + VenueCapabilities into the live path

**New primitives introduced:** `ExecutionMandate` (+ `MandateDecision`, `MandateRefused`)
in `scout/live/mandate.py`; `ExecutionReceipt` in `scout/live/receipts.py`;
`client_order_id_for_venue` / `VenueOrderIdForm` in `scout/live/order_id.py`;
`RoutedVenue` in `scout/live/routing.py`; DB column `live_trades.intent_hash`;
three new `reject_reason` values (`mandate_inactive`, `venue_capability_refused`,
`no_adapter_for_venue`).

Continues `3424436` (PRs #498/#499). Steps 9–12 of the enablement plan. Everything
here is Minara-credential-independent.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Content-hash identity for an internal dataclass | none found — hub catalog is client-rendered and did not enumerate on fetch (2026-08-02) | build in tree. `TradeIntent` already exists and is deployed; this plan only *derives* per-venue ids from its hash. No external capability could supply an invariant over our own frozen dataclass. |
| Exchange venue capability declaration | none found (same fetch limitation) | build in tree. `VenueCapabilities` is already deployed; the declarations are statements about **our adapters' implemented methods**, not about the venues' public APIs — no external catalogue can know that `KrakenSpotAdapter.send_order` raises. |
| Multi-venue order routing | none found (same fetch limitation) | build in tree. `RoutingLayer` already exists and reads project-owned SQLite views (`venue_listings`, `venue_health`). |
| Execution receipts / reconciliation | none found (same fetch limitation) | build in tree. Reconciliation is keyed on this project's `live_trades` / `solana_executions` schemas. |
| Autonomous-trading mandate / authorization gate | none found (same fetch limitation) | build in tree. A safety gate whose whole value is that it is *ours* and auditable must not be delegated to a third-party skill. |

Ecosystem check (awesome-hermes-agent): not reachable in this session beyond the
same client-rendered hub. **Verdict:** every domain above is an in-process
invariant over this repository's own types, adapters and schema — an external
skill is structurally incapable of supplying it, so the negative result does not
change the decision.

Drift-check (§7a) — searched the tree before scoping. Findings folded in below:
`RoutingLayer`, `KillSwitch`, `Gates`, `make_client_order_id`, `reconcile_open_live_trades`,
and the Solana `_check_autonomy_preconditions` multi-lock all already exist and are
**reused, not rebuilt**.

---

## The defect this closes (the reason the plan is shaped this way)

`scout/live/engine.py::_dispatch_live` routes with `RoutingLayer`, picks
`top = candidates[0]` … and then submits the order to `self._adapter` — the single
adapter handed to `LiveEngine` at construction, which is Binance. `top.venue` is used
only to label a counter increment.

So when routing selects a non-Binance venue, the order is placed **on Binance, using
the other venue's `venue_pair` string**. Routing is the lever that appears to control
venue selection; the constructor-injected adapter is the actual control. This is the
§9c lever-vs-data-path shape, and it is live on the `LIVE_USE_ROUTING_LAYER=True` path.

Three further gaps in the same path:

1. Nothing checks the selected venue *can do* the order (`permits_order` is deployed
   and unconsumed). Kraken declares limit-only; the dispatch path only ever places
   markets.
2. `intent_uuid = str(uuid4())` binds no terms — the gap `TradeIntent` was built to
   close, still unconsumed.
3. `_dispatch_live` is a fully autonomous entry path (paper signal → live buy, no
   human) with **no autonomy mandate at all**. The Solana lane has a three-lock
   promotion gate; the CEX path has none.

---

## Design

### 1. `scout/live/order_id.py` — per-venue id derived from the intent hash

One global truncated id is wrong (open follow-up #1). Instead, each venue declares a
*form* and the id is derived from `intent.intent_hash` into that form:

- `kraken` → `intent_hash[:32]` (32 hex; accepted by Kraken's own `_validate_cl_ord_id`
  via `_CL_ORD_ID_SHORT_UUID_RE`).
- `binance` → `gecko-{paper_trade_id}-{intent_hash[:8]}`, ≤ 28 chars.
- unknown venue → **raise**. Fail-closed: a venue with no declared form does not get a
  guessed one.

**28-vs-32 settled as far as the evidence allows.** Binance's spot trading-endpoints
doc (fetched 2026-08-02) documents `newClientOrderId` as "A unique id among open
orders… Orders with the same `newClientOrderID` can be accepted only when the previous
one is filled" and states **no maximum length and no regex**. So 28 remains
*unsourced* — but it is retained, because a shorter id cannot be rejected for length
under any limit that might exist. The change is strictly an improvement: the id is now
content-bound where it was random. Recorded, not papered over.

Consequence stated plainly: the Binance form carries 32 bits of hash, not 128. Two
intents sharing a `paper_trade_id` and an 8-hex prefix would collide. Kraken's 32-hex
form carries the full 128-bit prefix. The forms are **not** equally strong and the
module says so.

### 2. `scout/live/mandate.py` — the cross-venue authority

`ExecutionMandate` is venue-neutral and fail-closed. `authorize(intent)` returns a
`MandateDecision` or raises `MandateRefused`. It requires **all** of:

1. `LIVE_EXECUTION_MANDATE_MODE` is an executing mode (`SUPERVISED_LIVE` or
   `BOUNDED_AUTONOMOUS`). Default `DISABLED`.
2. A second explicit flag `LIVE_EXECUTION_MANDATE_ENABLED` (default `False`), so
   promotion takes two deliberate acts in two places — same shape as the Solana lane's
   second lock.
3. The intent's `venue_family` is in the mandate's declared families, and the venue is
   in the declared venue allowlist. Undeclared = absent.
4. For `BOUNDED_AUTONOMOUS` only: N prior supervised executions that actually reached a
   reconciled/filled terminal state, **counted from the ledger** per venue family
   (`solana_executions` for dex-solana, `live_trades` for cex). A flag cannot fake it.
5. A bounded envelope — per-trade max notional, daily notional, max open positions —
   each present and finite. `Decimal.is_finite()` before any comparison
   (`Decimal("Infinity") <= 0` is `False`).
6. The intent is not expired and its `mode` matches the mandate's mode.

Entry-only. **Exits are never mandate-gated**: the engine already refuses to boot
"can buy, cannot sell", and a mandate that blocked the closer would orphan real money.
Mirrors the Solana daily-cap-is-an-entry-cap finding.

### 3. Consuming paths

- `scout/live/routing.py` gains `select_route(intent)` → `RoutedVenue(candidate, adapter,
  capabilities)`. It runs the existing `get_candidates`, then for each candidate in rank
  order requires (a) an adapter exists for that venue, and (b)
  `adapter.describe_capabilities().permits_order(venue_family=…, order_type=…,
  reduce_only=…)` passes. First candidate satisfying both wins; every rejection is
  recorded with its reason. No candidate → a typed refusal naming why each was dropped.
- `scout/live/engine.py::_dispatch_live` builds the `TradeIntent`, calls
  `mandate.authorize(intent)`, calls `routing.select_route(intent)`, derives the cid via
  `client_order_id_for_venue`, and submits **through the returned adapter**. Refusals
  write a `rejected` `live_trades` row with the new reasons.
- `scout/live/solana_lane.py::_check_autonomy_preconditions` **additionally** consults
  the shared mandate after its own three locks pass. Additive strictness only — it can
  refuse a promotion the lane would have allowed, never allow one the lane would have
  refused. Today the lane is not in `BOUNDED_AUTONOMOUS`, so this path is unreachable
  and the change is inert; going forward it means "no venue promotes to autonomous
  without the cross-venue mandate."

### 4. `scout/live/receipts.py` — provider-neutral receipts

`ExecutionReceipt` normalizes a CEX `OrderConfirmation` and a Solana execution row into
one shape: `venue`, `venue_family`, `intent_hash`, `client_order_id`, `venue_ref`
(order id **or** transaction signature), `status`, `filled_quantity`, `fill_price`,
`raw`. `verify_binding(intent)` recomputes the venue's id form from the intent and
compares — a receipt whose id does not derive from the intent it claims is rejected.

`reconciliation.py` records the recovered receipt and checks that binding, so a
recovered row whose stored `intent_hash` does not derive the stored cid is
terminalized to `needs_manual_review` rather than resumed.

### 5. Schema

Migration `bl_venue_neutral_execution_v1`, `schema_version 20260802`:
- `ALTER TABLE live_trades ADD COLUMN intent_hash TEXT` + non-unique index.
- Extend the `reject_reason` CHECK on `shadow_trades` + `live_trades` with
  `mandate_inactive`, `venue_capability_refused`, `no_adapter_for_venue`, via the
  established rename-rebuild pattern (`_migrate_reject_reason_extend_v2`), including the
  `cross_venue_exposure` / `cross_venue_pnl` view drop-and-recreate.

Nullable, no DEFAULT: absence stays distinguishable from "written empty".

---

## Tests (step 12 is the load-bearing one)

- `tests/live/test_order_id.py` — per-venue forms, unknown-venue refusal, content
  binding (a changed term changes the id), Kraken form validates against the adapter's
  own `_validate_cl_ord_id`, Binance form ≤ 28.
- `tests/live/test_execution_mandate.py` — each lock refuses independently; ledger count
  cannot be faked by a flag; infinite/NaN envelope values refused; expired intent
  refused; mode mismatch refused.
- `tests/live/test_inactive_mandate_has_no_execution_path.py` — **step 12**. Behavioural
  *and* structural, following the two existing `*_has_no_autonomous_path` tests:
  with the mandate inactive, drive the real dispatch path against adapters and signer
  loaders built as `MagicMock(spec=…)` and assert **zero** calls to `place_order_request`
  / `place_limit_order` / `send_order` / any signer load / any broadcast; plus AST
  assertions that the mandate check cannot be bypassed (every submit site is downstream
  of an `authorize` call, and `authorize` is not called with a literal-true override).
- `tests/live/test_routing_capability_gate.py` — a candidate whose adapter is missing is
  dropped; Kraken (limit-only) is refused a market intent; the selected adapter is the
  one for the selected venue (the §9c defect, asserted directly).
- `tests/live/test_receipts.py` — binding verification, both providers.
- `tests/live/test_db_migration.py` — extend: fresh install and upgrade-with-data.

## Ship state

`LIVE_EXECUTION_MANDATE_MODE=DISABLED`, `LIVE_EXECUTION_MANDATE_ENABLED=False`.
No `.env` change is part of this PR. Nothing signs, broadcasts, or trades.
Positions untouched.

### FINAL STATUS — SHIPPED (2026-08-03)

Delivered across two PRs, both squash-merged and deployed to srilu-vps with
execution inactive:

| PR | Commit | Deployed | Scope |
|---|---|---|---|
| #500 | `ffe6eab3` | 2026-08-02 | Steps 9–12: `ExecutionMandate`, `RoutedVenue`/`select_route`, `client_order_id_for_venue`, `ExecutionReceipt`, `live_trades.intent_hash` |
| #501 | `cfd96c41` | 2026-08-03T00:38:31Z | 0x AllowanceHolder artifact + signing bundle, `scout/db_path.py` guard, Kraken entry TTY/content binding |

**Runtime-effective state verified on the deployed process** (not inferred from
source), 13 min uptime, `NRestarts: 0`, zero error-level lines since restart:

- boot log `execution_mandate_state`: `mode=DISABLED, flags_set=false,
  would_authorize=false, refused_at_gate=mode`
- `DisabledBroadcaster.submit()` → `BroadcastRefused` — "Nothing was sent"; it is
  the only broadcaster wired in the build
- no EVM key on disk, `EVM_SIGNER_KEY_PATH` unset; test identity
  `0x2dB703e30C186474B43Fa1dBF004655160e7Ef42` at **nonce 0**, 0 ETH, 0 WETH
  allowance to AllowanceHolder
- `LIVE_USE_ROUTING_LAYER=False`, so the adapter registration in `main.py` does
  not execute as deployed
- 0 EVM/0x rows in `live_trades`; positions unchanged

**Gates:** full suite run on the final commit *and* on `origin/master` in
parallel — 5616 vs 5382 passed with a **per-test failure diff empty in both
directions** (30 failed / 8 errors identical; the errors are Windows-environmental
— `fcntl`, `freezegun`, `telethon`). CI green. Four rounds of adversarial review
with mutation testing.

**Non-goals below still hold.** The Minara adapter remains blocked on
`MINARA_API_KEY`; no `.env` or flag was changed on the VPS.

### Follow-up debt carried out of this plan

Blocking the wiring PR, in order:

1. `TradeIntent.minimum_output` is `Decimal | None` in **human units** while the
   0x artifact takes an `int` in **base units**. That conversion is unwritten and
   is where a bug will live — a wrong decimals figure is off by 10^n either way.
   Too low is silent and unbounded; too high is loud and misreads as a venue
   problem, so the cheap failure gets diagnosed and the expensive one looks like
   a clean fill. Test at a non-18-decimals token (USDC=6 against WETH=18).
2. The intent floor is recorded on neither the artifact nor the bundle, so "what
   did we require?" is recoverable only by dereferencing the intent.

Then: rotate the 0x API key before any live use; `approval_for_artifact` for the
unbounded `required_amount`; `sign_bundle(PreparedExecution)` instead of a bare
bundle (the AST pin is a smoke alarm, not a boundary — `type(x)(**kw)` and
`x.__class__` evade it, and the name list was deliberately not extended); the
`adapters` map is typed `dict[str, ExchangeAdapter]` but holds an object that is
not one; Permit2 calldata decoding; `venue_listings.delisted_at` has no writer.

## Named non-goals

- Minara adapter (blocked on `MINARA_API_KEY`).
- Rewriting the Solana lane's own three locks — it keeps them and gains the shared
  mandate on top.
- Declaring `supports_reduce_only` for Binance (needs the held-size clamp first).
- Any `.env` / flag flip on the VPS.
