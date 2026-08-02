# 0x delivers a real unsigned-transaction artifact — probe results, 2026-08-02

**Status:** authentication and artifact retrieval CONFIRMED. Nothing signed, nothing
broadcast, no EVM wallet created. Read-only `/price` and `/quote` calls only.

Operator direction: **0x only.** 1inch is dropped (no key available).

## Why this matters

`scout/live/capabilities.py` defines `supports_unsigned_transaction` as "the venue
returns an artifact complete enough to validate and sign ourselves — chainId, nonce,
amountOutMin, expiry all present", and records that **nothing in tree declares it**,
with `docs/minara/MINARA_DEX_CAPABILITY_MATRIX.md` explaining why Minara does not
qualify: its CLI has zero transaction-construction surface, and `minara swap`
executes from Minara's own Privy-backed AA wallet — a custody model the operator
ruled out.

0x is the first provider probed that does qualify.

## What came back

Both v2 flavors returned HTTP 200 with `liquidityAvailable: true` and
`simulationIncomplete: false` (0x simulated the swap itself). Query was a real
mainnet route — 0.1 WETH → USDC, chainId 1 — using a known funded address as
`taker` purely so the quote would build. **We never sign for that address.**

| | `swap/permit2/quote` | `swap/allowance-holder/quote` |
|---|---|---|
| `transaction.to` | `0x0889e9327b98d7d1be3c301a4585ff3330502c9a` | `0x0000000000001ff3684f28c67538d4d072c22734` (AllowanceHolder) |
| `transaction.data` | 2450 hex chars, selector `0x1fff991f` | 3018 hex chars, selector `0x2213bc0b` |
| `transaction.gas` / `gasPrice` / `value` | present | present |
| `minBuyAmount` | `184651800` (vs `buyAmount` `186519800`) | same |
| `permit2.eip712` | `domain` / `types` / `primaryType` / `message` | absent |
| approval model | EIP-712 signature we produce locally | classic ERC-20 approve |

## The capability verdict, stated precisely

`transaction` carries **`to`, `data`, `value`, `gas`, `gasPrice`** — and
`minBuyAmount` is returned alongside it as a first-class field, which is the
`amountOutMin` the capability definition asks for. The permit2 flavor additionally
returns a full EIP-712 payload whose `message` carries the deadline, so the expiry
is inspectable before signing.

**`chainId` and `nonce` are NOT in the response, and that is correct rather than a
gap.** `chainId` is a request parameter — we supplied it, so we already hold it and
do not need it echoed to know what we asked for. `nonce` is wallet state that only
the signer can know. Both are fields the SIGNER legitimately owns; neither is
something the provider withheld. That is the distinction that separates 0x from
Minara, where the missing piece was the transaction itself.

So on the evidence: **0x qualifies for `supports_unsigned_transaction=True`.**
That declaration should still not be made until an adapter exists that actually
decodes and validates the calldata — declaring a capability from a probe rather
than from implemented behaviour is the inference bug `VenueCapabilities` exists to
kill (`adapter_base.describe_capabilities` is declared from what the adapter
implements, not from what the venue offers).

## Not yet established — the honest remainder

1. **Calldata decode.** The selectors above have not been decoded. Until the
   adapter can prove from the bytes that the recipient, the token pair and the
   minimum output are what the intent asked for, the artifact is unvalidated. This
   is the same discipline `scout/live/solana/tx_inspector.py` applies to Jupiter's
   build — check the transaction's own bytes, not the provider's summary.
2. **Fork simulation.** No local-fork `eth_call` has been run.
3. **No Gecko EVM identity exists.** Only `SOLANA_PILOT_SIGNER_PUBKEY` is present.
   An EVM signer, its custody model, and the `stat`-not-read discipline the Solana
   lane uses are all unbuilt.
4. **`issues.allowance.actual` is `"0"`** for the probe taker on both flavors, so a
   real execution needs an approval transaction first — a second signed action, and
   therefore a second authorization.

## Operational

- The probe ran **from srilu-vps**. This Windows workstation has no egress: `curl`
  returns HTTP 000 and Python TLS fails with
  `CERTIFICATE_VERIFY_FAILED … Basic Constraints of CA cert not marked critical`.
- The key was passed via SSH **stdin**, never argv and never written to disk on
  either host.
- **The key was pasted in plaintext into a chat transcript. Rotate it.**
- `ZEROEX_API_KEY` is not yet in any `.env`; no config change has been made.
