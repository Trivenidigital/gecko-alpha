# END_STATE_IMPLEMENTATION_PLAN — 2026-08-02

**New primitives introduced:** `TradeIntent` (canonical immutable content-hashed trade
intent), `VenueCapabilities` (explicit provider capability descriptor).

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trade intent canonicalization / hashing | none found — hub lists no intent-schema skill | build from scratch (product-defining; CLAUDE.md keeps intents/policy in-tree) |
| EVM unsigned-tx construction | Minara Agent API `intent-to-swap-tx` | **rejected** — artifact incomplete, see §2 |
| Solana swap build/sign/submit | `gecko-solana-verify` (adopted, SECONDARY_READ_ONLY) | already in use; execution stays in `scout/live/solana/` |
| CEX order routing | none found | existing `scout/live/` adapters retained |

awesome-hermes-agent ecosystem check: no maintained skill covers venue-neutral trade
intents or capability descriptors. Verdict: build the spine in-tree; adopt nothing new.

## §1 — What the drift check found (CLAUDE.md §7a)

Substantial parts of the requested end-state already exist. File:line evidence:

| Requested deliverable | Status | Evidence |
|---|---|---|
| Provider adapter contract | **EXISTS** (CEX-shaped) | `scout/live/adapter_base.py:57` `ExchangeAdapter` ABC |
| Venue router | **EXISTS** | `scout/live/routing.py:41` `RoutingLayer` |
| Kill switches | **EXISTS** | `scout/live/kill_switch.py:69` `KillSwitch` |
| Idempotency | **EXISTS** | `scout/live/idempotency.py:39` `make_client_order_id` |
| Reconciliation | **EXISTS** | `scout/live/reconciliation.py`, `scout/live/solana/execution_watchdog.py` |
| `BOUNDED_AUTONOMOUS` | **EXISTS, multi-lock** | `scout/live/solana_lane.py:2538-2610`; requires `SOLANA_BOUNDED_AUTONOMOUS_ENABLED` **and** N completed supervised executions **and** a bounded envelope |
| Signer isolation / inactive→no-signer | **EXISTS, stronger than specified** | `scout/live/solana_lane.py:98-109` — authorization binds the tx **message hash**; funded key is *not read* before authorization; pubkey from `SOLANA_PILOT_SIGNER_PUBKEY` |
| Kraken adapter | **EXISTS** (custom REST) | `scout/live/kraken_adapter.py:498` `KrakenSpotAdapter` |
| Solana Jupiter/Jito lane | **EXISTS** | `scout/live/solana/` (jupiter_client, tx_inspector, signer, jito_client) |

`scout/live/` is 27,121 LOC. The end-state is largely built.

**Genuine residual gaps** (partial matches are not closures, §7a):

1. **No content-bound intent.** `scout/live/engine.py:399` uses `intent_uuid = str(uuid4())` —
   a bare random UUID. `make_client_order_id(paper_trade_id, intent_uuid)` is idempotent with
   respect to the *identifier*, but nothing binds the intent's **terms** (asset, side, amount,
   venue, chain, slippage, recipient) to it. A mutated intent reuses the same identity.
2. **No capability descriptor.** Routing infers venue capability rather than reading it.
   `adapter_base.py` is Binance-shaped (`venue_pair`, `size_usd`, `fetch_exchange_info_row`)
   and carries no chain/DEX concepts.

These two are what this branch builds. They are venue- and chain-neutral, so they are
unblocked by every connector finding in §2.

## §2 — Connector rulings (evidence, not judgment)

### Minara DEX — BLOCKED, by the operator's own stop condition

Stop condition invoked: *"Minara cannot produce a host-validatable artifact for that chain."*

The capability lives **only** at `POST api-developer.minara.ai/v1/developer/intent-to-swap-tx`.
Documented response: `unsignedTx { chainType, from, to, data, value, gas, gasPrice,
maxPriorityFeePerGas }`.

Absent — each load-bearing, each required by the operator's own EVM validation list:

| Missing | Consequence |
|---|---|
| `chainId` | no EIP-155 replay protection — a signature valid on one chain is valid on another |
| `nonce` | deriving it ourselves means the artifact approved ≠ the artifact signed |
| `amountOutMin` | the slippage bound is **not in the thing we would validate**; slippage is not a caller input |
| expiry | nothing bounds how long an approved artifact stays valid |
| `maxFeePerGas` | incoherent legacy/EIP-1559 mix |

Runtime probe requires a paid Pro API key we do not hold → `UNKNOWN_PENDING_PRO`. No chain is
`RUNTIME_PROVEN`. Evidence: `tasks/overnight_2026_08_02/MINARA_EVM_CAPABILITY_MATRIX.md`.

### Minara CLI / skill install — NOT ON THE PATH TO THE GOAL

Source-proven (zero hits for `calldata|nonce|signTransaction` across the published package):
**installing the CLI would not obtain the unsigned-tx capability.** "Should we install the CLI"
and "can Minara build us an unsigned EVM transaction" are independent questions.

Installing it would import three defects verified from the `minara@0.4.7` tarball
(sha1 `f1077dc2…`, matching registry):

- `dist/utils.d.ts:61-66` documents the transaction confirmation as *"independent of the `-y`
  flag"*; `dist/commands/swap.js:115-118` guards it with `if (!opts.yes)`. Same guard on
  `withdraw.js:81`, `transfer.js:56`, `perps.js` (7 sites).
- `dist/touchid.js:148-155` returns early on non-darwin. The VPS is Linux → on `-y`, zero gates.
- `dist/config.js:52-62` spreads `~/.minara/config.json` over defaults unvalidated → `baseUrl`
  is overridable with no scheme check, host allowlist, or TLS assertion, redirecting the Bearer
  token and every trade to an arbitrary host.

The third maps directly onto the stop condition *"the wallet, chain, recipient or amount may
change silently."* It can.

**Ruling: do not install.** Should the artifact gap ever close, the correct integration is a
direct pinned-host HTTPS call from Gecko's own aiohttp client — which routes around findings
1–3 entirely, because none of them are in that path. Installing the CLI has negative expected
value: it obtains no capability and imports three defects.

### Kraken MCP — DOES NOT EXIST

Re-verified 2026-08-02 on `srilu-vps`: no `*kraken*mcp*` anywhere under depth 6, no `mcp.json`
in the active Hermes home. Confirms the 2026-05-30 phantom-precondition finding, still true.

There is no MCP to adapt to. `KrakenSpotAdapter` (`scout/live/kraken_adapter.py:498`, custom
REST, full `ExchangeAdapter` impl with nonce management, ambiguous-submission handling, and
withdrawal-capability refusal) **is** the Kraken path. Deliverable "Kraken MCP adapter" is
satisfied-by-absence: the thing it would replace does not exist, and the incumbent is proven.

### Other CEX connectors

`binance_adapter.py`, `ccxt_adapter.py` exist. Credential state and per-venue enablement are
recorded in `docs/minara/CEX_CONNECTOR_CAPABILITY_MATRIX.md`.

## §3 — Runtime baseline (2026-08-02)

- Deployed HEAD `2128b457`, `gecko-pipeline` + `gecko-dashboard` both `active`
- Hermes: `gecko-agent`, `/home/gecko-agent/.hermes`, gateway running (pid 1912637)
- Minara: **absent** — no binary on PATH for either user, no `~/.minara` for either user
  (`config.js::ensureDir()` creates it on first run, so its absence is decisive)
- `LIVE_MODE=shadow`, `SOLANA_MODE=SUPERVISED_LIVE`, `MINARA_ALERT_ENABLED=false`,
  `KRAKEN_PILOT_ENABLED=true`
- Positions untouched this session; no order, signature, or broadcast issued

## §4 — What this branch delivers

1. `scout/live/intent.py` — canonical immutable `TradeIntent` + deterministic `intent_hash`
2. `VenueCapabilities` descriptor + `describe_capabilities()` on the adapter contract
3. Tests: canonicalization determinism, hash sensitivity to every material field, immutability,
   expiry, reduce-only invariants, capability honesty

Deliberately **not** delivered, with reasons above: Minara install, Minara DEX adapter, Kraken
MCP adapter. Each is a named connector gap, not an unfinished task.
