# Minara per-chain unsigned-transaction capability matrix (2026-08-01)

Purpose: answer the one load-bearing question for the permanent Minara EVM
safety path — **can Minara emit an unsigned transaction artifact that we sign
locally?**

The required path is unchanged:

```
Minara unsigned transaction
  -> deterministic LOCAL decoding
  -> allowlist and value checks
  -> independent simulation
  -> human authorization
  -> LOCAL signing
  -> controlled broadcast
  -> receipt and balance reconciliation
```

Method: local repo evidence plus read-only fetches of Minara's published
documentation (including the full 453 KB `llms-full.txt` corpus). **No account
was created, no credentials used, no state changed, no repo file modified.**

---

## 1. Headline — the May-2026 custody finding is partially superseded

`tasks/findings_minara_verification_2026_05_06.md` established that Minara is
custodial, which is why it was removed from the August execution path. That
finding remains correct **for the product and CLI lane**.

It is now incomplete. Minara has since shipped a developer **Agent API** whose
swap endpoint returns an **explicitly unsigned EVM transaction object** for a
caller-supplied wallet address. The question "can Minara emit something we sign
ourselves?" therefore has a documented **yes, on EVM** that did not exist when
the prior ruling was made.

**Credit where due:** the 2026-07-26 live-DEX verification already reached the
right shape — it recorded that the Agent API "returns an executable on-chain
transaction payload" and drew the correct conclusion that **the signer and
broadcaster are on our side**, so MEV protection is a property of *our*
broadcast path. This document does not overturn that. What is new here is the
concrete artifact schema and its gaps, the per-chain proof, the Solana and
Robinhood proven negatives, and the paid-tier constraint.

This does **not** reverse the decision to build our own Solana lane — see §3,
where the Solana answer is a proven no.

---

## 2. The matrix

| Chain | Minara supports it? | Unsigned tx artifact? | Evidence | Status |
|---|---|---|---|---|
| ethereum | yes | **yes** | `intent-to-swap-tx` returns `unsignedTx{chainType,from,to,data,value,gas,gasPrice,maxPriorityFeePerGas}`, described as "Unsigned transaction ready to be signed and broadcasted" | PROVEN doc-level, **UNPROVEN at runtime** |
| base | yes | **yes** | same endpoint, `chain:'base'` documented | PROVEN doc-level, UNPROVEN at runtime |
| bsc | yes | **yes** | same endpoint, `chain:'bsc'` documented | PROVEN doc-level, UNPROVEN at runtime |
| arbitrum | yes | **yes** | same endpoint, `chain:'arbitrum'` documented | PROVEN doc-level, UNPROVEN at runtime |
| optimism | yes | **yes** | same endpoint, `chain:'optimism'` documented | PROVEN doc-level, UNPROVEN at runtime |
| **solana** | yes (spot trading) | **NO** | `walletAddress` is `0x…`; `unsignedTx` is EVM-shaped; no endpoint anywhere returns a Solana payload | **PROVEN NEGATIVE** (doc-level) |
| polygon | **contradictory** — absent from the supported-chains FAQ, present in the CLI chain list | no evidence | FAQ lists 6 chains without polygon; CLI README lists it | UNPROVEN |
| avalanche | **contradictory** — same as polygon | no evidence | same | UNPROVEN |
| zksync | not listed anywhere | no evidence | 0 hits across the full docs corpus | UNPROVEN (likely unsupported) |
| **Robinhood Chain** | **no** | **no** | **0 occurrences of "Robinhood"** across the entire 453 KB docs corpus, both API references, and the CLI/Skills READMEs | **PROVEN NEGATIVE** |
| Berachain, Blast, Manta, Mode, Sonic, Conflux, Merlin, Monad, XLayer | CLI/Skills lane only | no — the CLI executes through Minara's controller wallet and emits no artifact | CLI README; `findings_minara_verification_2026_05_06.md:25` | UNPROVEN |

### Endpoint facts

```
POST https://api-developer.minara.ai/v1/developer/intent-to-swap-tx
Authorization: Bearer <API_KEY>

request : intent (natural language, required), walletAddress (0x…, required),
          chain (optional, FREE-FORM STRING — no enum; the five names above are
          documented examples, not an enumeration)
response: intent, quote{…}, inputToken, outputToken, unsignedTx{…},
          approval{isRequired, tokenAddress, spenderAddress, requiredAmount,
                   approveAmount, currentAllowance, message}
```

Endpoint description, verbatim: *"Convert natural language trading intent into an
executable swap transaction payload. **Compatible with OKX DEX by default.**"*

The pay-per-call **x402 variant returns no `unsignedTx`** — only a transaction
descriptor. Unsigned-tx emission is **API-Key flow only, which requires a paid
Pro plan.**

---

## 3. Verdict

Minara **can** occupy the unsigned-transaction slot of our safety path — but
only on EVM, only through the paid API-Key flow, and only with three gaps we
must close ourselves.

The artifact is genuine and correctly shaped for local signing (`to` / `data` /
`value` / `gas`), and it is emitted for a **caller-supplied** wallet address,
so it is compatible in principle with our decode → allowlist → simulate →
authorize → local-sign → broadcast chain. The gaps:

1. **No `chainId` and no `nonce`**, and `maxPriorityFeePerGas` without
   `maxFeePerGas`. It is an incomplete EIP-1559 transaction. "Sign it as
   received" is impossible; our local builder becomes load-bearing.
2. **The ERC-20 `approve` step is metadata only**, not a second unsigned
   transaction. We build approve calldata ourselves.
3. **The calldata targets the OKX DEX router by default**, so our allowlist
   becomes an OKX-router allowlist.

The residual trust question is: *do we trust a natural-language intent string to
be faithfully compiled into router calldata by a remote service?* That is
precisely what deterministic local decoding plus independent simulation exist to
check, so the safety path remains sound — but it is now the load-bearing control,
not a formality.

**On Solana the answer is a flat no.** There is no Solana unsigned-transaction
artifact anywhere in the API. This independently vindicates having built our own
Jupiter + local-sign + Jito lane.

The CLI/Skills lane verified in May still emits nothing signable — it executes
through Minara's custodial controller wallet — so nothing about the existing
alert-only design in `scout/trading/minara_alert.py` changes.

---

## 4. What remains UNPROVEN, and what would settle it

| Unproven | Evidence that settles it |
|---|---|
| The endpoint accepts an **arbitrary external EVM address** (our own locally-keyed wallet) rather than only a Minara-hosted funding wallet. **This is the single most important open question** — if it does not, the whole path collapses. | One live `POST /v1/developer/intent-to-swap-tx` with our address. **Needs a Minara Pro subscription and API key — operator spend plus credentials.** The docs never state the constraint either way. |
| Actual chain coverage beyond the five documented examples | One live call per chain. The docs give no enum. |
| Whether `unsignedTx` carries `chainId` / `nonce` in practice despite their absence from the schema | One live response body. |
| Whether the calldata is deterministic enough for our decode + allowlist gate (router-address stability, slippage encoding) | ≥3 live responses plus on-chain simulation. |
| Minara's own docs **contradict themselves** on chain count — an "8 chains" marketing claim versus a 6-chain FAQ | Product-side confirmation; not resolvable from documentation. |
| Webhook / alert intake for a Gecko→Minara push | **No webhook capability exists in the docs corpus.** Would need a product-roadmap answer from Minara. |

### Documented drift worth re-adjudicating

`minara.ai/docs/technology/wallet-security.md` now describes a **"non-custodial"**
ERC-4337 smart wallet (Privy-backed) with an M-of-N / TEE **controller wallet
operated by Minara**, and `export-private-key.md` documents user-initiated
private-key export for both EVM and Solana spot wallets (max 3 per 24 h,
TOTP-gated).

This is **operationally custodial** — Minara's controller signs — but it is not
key-inaccessible, which is a weaker claim than the May finding assumed. Recorded
as drift for the operator to re-adjudicate; **not** treated here as overturning
the prior ruling.

---

## 5. Robinhood Chain

**Not supported by Minara, and no evidence of any plan.** Zero occurrences of
"Robinhood" across Minara's entire published corpus.

Robinhood Chain is genuinely live (an Ethereum L2 launched 2026-07-01, with
Robinhood extending MCP-based agentic trading to crypto on 2026-07-20), so this
is a real coverage gap rather than a stale-chain artifact.

It is **also absent** from the official Hermes EVM skill's eight chains. So
Robinhood Chain has **no unsigned-transaction path on either side today**;
anything targeting it needs a bespoke lane. It stays unsupported until proven.
