# Hermes skills inventory — Gecko-Alpha (srilu-vps)

Scope: **srilu-vps only.** This is a bounded, Gecko-specific adoption, not a
fleet-wide programme. No other VPS was touched. Hermes Agent v0.19.1 @
`cc4cab2f`, `HERMES_HOME=/home/gecko-agent/.hermes`.

Installed 2026-08-01 with `--yes` only. **`--force` was never used.**

---

## 1. Adoption record

| Full identifier | Version / author | Tree SHA-256 (first 32) | Hermes scan verdict | Gecko classification (final) |
|---|---|---|---|---|
| `official/software-development/rest-graphql-debug` | 1.2.0 / eren-karakus0 | `0905d084803548b58f5b0c8f87c7385a` | **DANGEROUS** | **QUARANTINED_REFERENCE_ONLY** |
| `official/blockchain/solana` | 0.2.0 / Deniz Alagoz (gizdusum), enhanced by Hermes Agent | `1739e4b54d0e5b150cdaf123bc73f748` | CAUTION | **APPROVED_SECONDARY_READ_ONLY — ONLY THROUGH THE GUARDED RESOLVER WRAPPER** |
| `official/blockchain/evm` | 1.0.0 / Mibayy, youssefea, ethernet8023, Hermes Agent | `28e9f025feb23c384dd0718305cd29cf` | SAFE | **RESTRICTED_REFERENCE_ONLY — NOT ACCEPTABLE FOR MINARA AUTHORIZATION OR PRE-SIGN VALIDATION** |

Per-file hashes (first 16):

```
rest-graphql-debug/SKILL.md          4921a93f47056bb8   15602 B
blockchain/solana/SKILL.md           6ab50db27b977121    6429 B
blockchain/solana/scripts/solana_client.py
                                     11ee24293fdf3ec1   26039 B
blockchain/evm/SKILL.md              cd1604e4b0712bd7    8410 B
blockchain/evm/scripts/evm_client.py 96a0187de7efd535   55882 B
```

**Name ambiguity, resolved.** `solana` is not a unique name — five skills share
it (one official, four community across skills.sh / clawhub). Full identifiers
are used everywhere in this document and must be used in any install command.

**Gecko production skills verified unchanged after install** (no collision, no
overwrite): `coin_resolver 5678b813…`, `crypto_narrative_scanner 94daa940…`,
`kol_watcher 8d97dbdc…`, `narrative_alert_dispatcher 126680c3…`,
`narrative_classifier be870e1e…`.

---

## 2. Governance finding — an official source does not neutralise a DANGEROUS verdict

Hermes scanned `rest-graphql-debug` and returned **DANGEROUS**, then admitted it
anyway. The recorded decision was:

```
Decision: ALLOWED — Allowed (builtin source, dangerous verdict)
```

That is: **source provenance overrode the behavioural verdict.** The scan was
not wrong — the skill's documentation contains 19 references to POST/PUT/DELETE,
`curl -X`, and `Authorization: $TOKEN` — so DANGEROUS is inherent to what the
skill teaches, and it is not a false positive to be tuned away.

**Fleet policy (recorded here, to be implemented as an admission policy):** an
official source may establish *provenance*, but it must not override a
*behavioural* verdict. A DANGEROUS skill must be blocked or quarantined even
when Hermes itself auto-allows it. This is a Hermes admission-policy gap, and it
is upstream of Gecko — recorded, not worked around, and deliberately **not** a
reason to reopen fleet architecture work.

---

## 3. `official/software-development/rest-graphql-debug` — QUARANTINED_REFERENCE_ONLY

Ships `SKILL.md` only. **No scripts, no executable payload.** Its danger is
instructional: an agent that follows its examples verbatim will issue
authenticated mutating HTTP requests.

Standing Gecko restrictions:

- Read-only verbs by default.
- Never against order, transaction, approval, withdrawal, or credential-mutation
  endpoints.
- Mutating requests require explicit human authorization obtained **outside**
  Hermes.
- Redact `Authorization` headers and payload secrets in all output.
- **Never execute an example command merely because it appears in the skill.**

Diagnostic probe run (read-only, GET only, no `Authorization` header sent,
nothing mutating attempted) against the local dashboard:

```
GET http://127.0.0.1:8000/api/health   -> http_code=404 time=0.0029s 22 B application/json
                                          body: {"detail":"Not Found"}
GET http://127.0.0.1:8000/api/definitely-not-a-route -> http_code=404
```

Both the probe and its negative control return 404 with an identical body, so
this probe demonstrates connectivity and response shape but **does not**
discriminate a live route from a missing one — `/api/health` simply does not
exist on this dashboard. Recorded as-is rather than re-run against a route
chosen to make the probe look successful.

---

## 4. `official/blockchain/solana` — APPROVED_SECONDARY_READ_ONLY

Read-only verification: `solana_client.py` scores **0** for signing and
broadcast patterns. Subcommands are `stats, wallet, tx, token, activity, nft,
whales, price` — no send, no sign, no submit.

Configured to Gecko's approved private resolver via `SOLANA_RPC_URL`
(`solana-mainnet.g.alchemy.com`), **not** the default public endpoint.

### Probe: wallet — agrees exactly with direct RPC

| Field | Skill | Independent evidence | Match |
|---|---|---|---|
| SOL balance | `0.10145672` | wallet + ledger | exact |
| USDC | `6.899618` (mint `EPjFWdd5…yTDt1v`) | `getTokenAccountsByOwner` → `6.899618` | exact |
| NFTs / token-2022 | `0` | 0 token-2022 accounts | exact |

### Probe: transaction `3oUrwFDL…pj1M3` — decomposition closes to the lamport

Slot `436606196`, status success, fee `5e-06` SOL (5,000 lamports) — all match
the authoritative record. The balance decomposition independently explains the
**entire** SOL delta:

```
0.096       swap input      -> 44X92BsH…988qQ   == amount_lamports 96,000,000 in solana_executions
0.0005      Jito tip        -> 3AVi9Tg9…KZ6jT
0.00203928  ATA rent        -> FeJcMQtR…rZTG    (rent-exempt minimum, USDC account created)
0.000005    base fee
----------
0.09854428  == the wallet's observed SOL change, exactly
```

Programs invoked include Jupiter v6 (`JUP6LkbZ…VTaV4`), consistent with the
recorded route. This is genuine independent corroboration, not a restatement.

### One recorded discrepancy (pre-existing, not caused by the skill)

Ledger row 5 has `size_usd = 6.898692` while `entry_fill_qty × entry_fill_price
= 6.899618` (the executed USDC out). The difference (0.000926 USDC) is the
quoted-versus-executed gap — the trade filled 1 bp *better* than quoted. This is
the already-recorded debt item "`size_usd` = authorized ceiling, not executed
notional", confirmed here from a second direction.

### Caveat on the skill's USD columns

`sol_price_usd` moved 71.88 → 71.93 → 71.90 across three calls seconds apart.
The USD-valued fields are live-marked and **must never be used for
reconciliation**. Only the lamport / token-unit fields are ledger-grade.

### Silent public-RPC fallback — found, and now guarded

`solana_client.py:31-33` defaults `SOLANA_RPC_URL` to
`https://api.mainnet-beta.solana.com`. Verified at runtime with the variable
unset: **exit 0, byte-identical output, no warning on stdout or stderr.** An
operator who forgets the variable gets plausible-looking evidence from an
unapproved endpoint with no indication.

Mitigation shipped: `/usr/local/bin/gecko-solana-verify` wraps the skill and
refuses (exit 78) unless the approved resolver is present in `.env`, is not the
public default, and answers `getGenesisHash` with the Solana mainnet genesis
`5eykt4Us…dw2N9d`. Both refusal paths were tested; `.env` was verified
byte-identical afterwards.

### Trust boundary

Gecko's deterministic reconciliation in `scout/live/solana/` remains
**authoritative**. This skill must never select a route, approve a transaction,
compute authorization limits, sign, submit through Jito, or resolve ambiguous
submission state.

**The skill is approved only when invoked through the guarded wrapper.** Direct
invocation of `solana_client.py` is not approved, because it reintroduces the
silent public-RPC fallback.

### Wrapper provenance — repo is the source of truth

The wrapper is version-controlled at `ops/gecko-solana-verify` (mode 755) and
deployed by `ops/deploy-ops-tools.sh`, which is idempotent and supports
`--check` for drift detection. It must not exist only as an unmanaged file on a
VPS.

Deployed parity verified 2026-08-01:

```
repo blob          sha256 de40595cf29ab8858dc45ad1beecb970b6500bd66f5c64af7128cd8d305a0478
/usr/local/bin/…   sha256 de40595cf29ab8858dc45ad1beecb970b6500bd66f5c64af7128cd8d305a0478
mode 755, 2870 bytes, LF-only — byte-identical
```

---

## 5. `official/blockchain/evm` — RESTRICTED_REFERENCE_ONLY

**Final classification: `RESTRICTED_REFERENCE_ONLY`. Not acceptable for Minara
authorization or pre-sign validation.** This is no longer "inconclusive" — the
limitations below are proven, not suspected:

- it can hang indefinitely in CoinGecko enrichment (§5.1);
- it leaks queried public-address activity to an external pricing service (§5.1);
- it accepts mined transaction hashes only (§5.3);
- it therefore cannot inspect Minara's unsigned pre-sign artifact (§5.3);
- proxy detection misses legacy ZeppelinOS proxies (§5.2);
- proxy detection misses EIP-7702 delegation (§5.2).

It may still be used for **non-sensitive public-address investigation**. It must
remain outside every safety and authorization decision.

### Evidence

Read-only verification: `evm_client.py` scores **0** for signing and broadcast
patterns. Chains configured: `arbitrum, avalanche, base, bsc, ethereum,
optimism, polygon, zksync`. **Robinhood Chain is absent.**

### 5.1 The empty probe — a hang, not an empty success

```
command: …/evm_client.py multichain 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
exit   : 124 (still running when killed; reproduced at 180 s and 240 s)
stdout : 0 bytes
stderr : 0 bytes normally; 4607 bytes of stack under faulthandler
chain  : all 8 (multichain fans out)
```

Stack at abort, identical across worker threads:

```
_http_get (evm_client.py:367)
  <- cg_price_by_contract (evm_client.py:586)
    <- scan_chain (evm_client.py:1125)
```

Root cause: it blocks on **CoinGecko price enrichment**, not on EVM RPC. Single
CoinGecko calls succeed (`stats`, `gas` return `native_price_usd` fine); the
per-token fan-out across 8 chains is what stalls. Two usability defects follow:
`_http_get` has no timeout on that path, and output is buffered to the end, so a
kill yields zero bytes despite work having been done.

Side effect worth recording for Minara: a `wallet` query **transmits the queried
address and its token contracts to `api.coingecko.com`**. Address inspection is
not local.

### 5.2 Proxy detection — PROXY_DETECTION_FALSE_NEGATIVE

Detection is EIP-1967 implementation slot plus an EIP-1167 bytecode prefix, and
nothing else (`evm_client.py:1315-1326`). Five fixtures, each checked against
raw `eth_getCode` and three storage slots:

| Fixture | Address | Skill says | Independent evidence | Verdict |
|---|---|---|---|---|
| True EOA | `0x…dEaD` | `is_contract: false` | code `0x`, 0 bytes | **correct** |
| EIP-7702 delegated account | `0xd8dA…6045` (vitalik.eth) | `is_contract: true`, `is_proxy: false`, `implementation: null` | code = `0xef0100` ‖ `5a7fc113…f96f6d`, 23 B — an EIP-7702 delegation | **FALSE NEGATIVE** |
| Non-proxy contract | `0xC02a…6Cc2` (WETH9) | `is_proxy: false` | all three slots zero | **correct** |
| Legacy-slot proxy | `0xA0b8…eB48` (USDC) | `is_proxy: false`, `implementation: null` | EIP-1967 slot zero, **zeppelinos legacy slot = `0x43506849d7c04f9138d1a2050bbf3a0c054402dd`** | **FALSE NEGATIVE** |
| EIP-1967 proxy | `0x8787…fA4E2` (Aave V3 Pool) | `is_proxy: true`, `implementation: 0x728a138a…fe03cf` | EIP-1967 slot = same address | **correct** |

Two distinct false-negative classes: **legacy/custom proxy storage
conventions** and **EIP-7702 delegations**. A zero EIP-1967 slot does not prove
"not a proxy". USDC still reported `detected_standards: ["ERC-20"]` because the
proxy delegates the calls — so the standards field can look right while the
proxy field is wrong, which is the dangerous combination.

**`is_proxy` and `implementation` are disabled for Minara safety decisions.**

### 5.3 Transaction decoding — mined hashes only

`cmd_decode` calls `require_txhash(args.hash)` then
`eth_getTransactionByHash`. Capability matrix:

| Input form | Supported |
|---|---|
| Mined transaction hash | **yes** |
| Unsigned serialized transaction | no |
| Destination / value / calldata tuple | no |
| Raw calldata | no |

Decoding depth is also thin: a 4byte.directory selector lookup plus a raw
preview, with `transfer(address,uint256)` as the only special case.

**Consequence:** the skill can only decode transactions that have *already been
mined*. It therefore **cannot validate Minara's unsigned pre-sign artifact — the
single most important object in the authorization gate — and must not be used
for that gate.**

### 5.4 Role

Optional secondary inspection. Not a prerequisite for Minara, and not inside the
trust boundary until the above is resolved. Investigation is time-boxed and
closed here; no repair work was undertaken on an optional skill.

The permanent Minara EVM safety path is unchanged:

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

---

## 6. Follow-ups

1. Add an organization-level admission policy that blocks or quarantines
   `DANGEROUS` skills even when Hermes auto-allows them on source provenance.
   (Policy stated in §2; implementation not yet done.)
2. `org/gecko-supervised-trade-reconciliation` is **DRAFT** at
   `docs/skills-draft/org/gecko-supervised-trade-reconciliation/` and is
   deliberately **not installed** into Hermes until fixture-tested.
3. If the EVM skill is ever promoted beyond optional: add legacy/custom proxy
   slots and EIP-7702 detection, a timeout on the CoinGecko path, and an
   unsigned-transaction decode input.
