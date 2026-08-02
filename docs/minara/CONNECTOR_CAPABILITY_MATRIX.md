# CONNECTOR_CAPABILITY_MATRIX — 2026-08-02

Consolidates the DEX and CEX matrices. Every "blocked" row names a specific technical
cause and the operator stop-condition it fires, not a judgment call.

## DEX

| Chain | Documented | API accepted | External wallet | Artifact returned | Fully decodable | Simulatable | Gecko signer | Broadcast adapter | Reconciliation | **Enabled** | Reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Ethereum | yes | untested | untested | doc only | **no** | untested | no | no | no | **disabled** | artifact incomplete |
| Base | yes | untested | untested | doc only | **no** | untested | no | no | no | **disabled** | artifact incomplete |
| BSC | yes | untested | untested | doc only | **no** | untested | no | no | no | **disabled** | artifact incomplete |
| Arbitrum | yes | untested | untested | doc only | **no** | untested | no | no | no | **disabled** | artifact incomplete |
| Optimism | yes | untested | untested | doc only | **no** | untested | no | no | no | **disabled** | artifact incomplete |
| Solana (Minara) | **not supported** | — | — | — | — | — | — | — | — | **n/a** | Minara does not offer it, and it is not wanted |
| **Solana (Gecko lane)** | — | — | — | **yes** | **yes** | **yes** | **yes** | **yes (Jito-only)** | **yes** | **ENABLED, unchanged** | proven 2026-08-01 |

**Nothing is `RUNTIME_PROVEN` for Minara. Not one chain.** No row rests on an executed call.

### Why every Minara EVM row is disabled

The capability exists only at `POST api-developer.minara.ai/v1/developer/intent-to-swap-tx`
— not in the CLI, not in the skill (source-proven: zero hits for
`calldata|nonce|signTransaction` across the published package).

Documented response: `unsignedTx { chainType, from, to, data, value, gas, gasPrice,
maxPriorityFeePerGas }`.

| Missing field | Why it is load-bearing |
|---|---|
| `chainId` | no EIP-155 replay protection — a signature valid on one chain is valid on another |
| `nonce` | deriving it ourselves means the artifact approved is not the artifact signed |
| `amountOutMin` | **the slippage bound is not in the thing we would validate**; slippage is also not a caller input |
| expiry | nothing bounds how long an approved artifact stays valid |
| `maxFeePerGas` | incoherent legacy/EIP-1559 mix |

Stop condition fired: *"Minara cannot produce a host-validatable artifact for that chain."*
The operator's own EVM validation list requires chain ID, nonce, minimum output, deadline
and visible slippage. Four are absent from the documented shape.

This conclusion does not depend on the blocked probe. Running the probe would establish
whether the endpoint behaves as documented; it would not add the missing fields. The probe
is separately blocked at `UNKNOWN_PENDING_PRO` — it needs a paid Minara Pro API key.

### Why the CLI was not installed

Installing it would not obtain the capability above — the two questions are independent,
and the source answers the first one no. It would import three defects verified from the
`minara@0.4.7` tarball (sha1 `f1077dc25122fa951aa3d45214bce9ecaea3e7c2`, matching registry):

1. `dist/utils.d.ts:61-66` documents the confirmation as *"independent of the `-y` flag"*;
   `dist/commands/swap.js:115-118` guards it with `if (!opts.yes)`. Same guard on
   `withdraw.js:81`, `transfer.js:56`, `limit-order.js:89`, `perps.js` (7 sites).
2. `dist/touchid.js:148-155` returns early on non-darwin. The VPS is Linux → under `-y`,
   zero gates remain.
3. `dist/config.js:52-62` spreads `~/.minara/config.json` over defaults unvalidated →
   `baseUrl` overridable with no scheme check, host allowlist or TLS assertion, redirecting
   the Bearer token and every trade to an arbitrary host. This fires the stop condition
   *"the wallet, chain, recipient or amount may change silently."*

Installing has negative expected value: no capability gained, three defects imported.

**If the artifact gap ever closes**, the correct integration is a direct pinned-host HTTPS
call from Gecko's own `aiohttp` client. That routes around defects 1–3 entirely, because
none of them sit in that path. It also satisfies the operator's requirement that free-form
`minara withdraw|transfer|perps|sweep|autopilot` never be reachable from autonomous
routing — the strongest form of which is not installing the command surface at all.

## CEX

| Venue | Connector | Credentials | Order forms declared | Cancel | Client order ID | Partial fills | Withdrawal | **Enabled** |
|---|---|---|---|---|---|---|---|---|
| Kraken | `kraken_adapter.py:498` `KrakenSpotAdapter`, custom REST | present (**rotate — see RUNTIME_BASELINE**) | **limit only** | yes | yes (≤18 ASCII / 32 hex / UUID) | yes | **refused in code** | enabled, pilot |
| Binance | `binance_adapter.py:76` `BinanceSpotAdapter` | not verified this session | market | not declared | yes | yes | not declared | unchanged |
| CCXT scaffold | `ccxt_adapter.py` | n/a | none declared | — | — | — | — | scaffold only |
| **Kraken MCP** | **does not exist** | — | — | — | — | — | — | **n/a** |

### Kraken declares limit-only, and that is not a downgrade

`kraken_adapter.py` module docstring item 8: `send_order`, `place_order_request` and
`place_exit_order` **all raise**; `place_limit_order` is the only method that reaches
`AddOrder`. The descriptor now says so. Previously routing would have inferred market
support from the presence of the method and been wrong at call time.

### Kraken MCP

Does not exist on the host (see RUNTIME_BASELINE). The deliverable "make Kraken MCP the
preferred Kraken execution provider" has no provider to prefer. `KrakenSpotAdapter` — with
nonce management, `KrakenAmbiguousSubmissionError` handling, local `cl_ord_id` validation
and explicit withdrawal refusal — is the Kraken path, and no code was removed in favour of
something that is not there.

## Money movement

No connector in tree declares `supports_withdrawal`, `supports_transfer` or
`supports_sweep`. `VenueCapabilities.MONEY_MOVEMENT` enumerates them separately and no
trading path consults them; a test asserts the enumeration stays complete.
