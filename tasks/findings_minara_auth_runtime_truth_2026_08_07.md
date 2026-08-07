# Minara auth/runtime truth — the `MINARA_API_KEY` blocker was false

**Date:** 2026-08-07
**Method:** read-only. Source + packaged-artifact inspection, `npm view` in an
isolated `HOME`. No install into production, no login, no fund-moving command.
No `~/.minara` was created; the installed tree was not modified.

**Ruling:** `MINARA_AUTH_PATH_UNSAFE`
**Owner action required:** **NONE.** The previously recorded owner dependency
was a phantom.

---

## 1. The blocker was never real

Every prior record said the Minara lane was "blocked ONLY on owner
`MINARA_API_KEY`". Three independent artifacts agree it is not:

| Artifact | Version | `MINARA_API_KEY` hits | `apiKey\|api_key` hits | env vars read |
|---|---|---:|---:|---|
| Installed (`/home/gecko-agent/.minara-cli`) | 0.4.7 | **0** | **0** | `DEBUG` |
| npm current (`npm view minara version`) | **0.4.7** | **0** | **0** | `DEBUG` |
| GitHub source checkout | 0.4.7 | **0** | **0** | `DEBUG` |

Integrity of installed and npm-current match exactly:
`sha512-K9NOs3VBiNBOShNYb0+kHt3dfVNQRn9/KpPZ87tlOuh3xNYjD3Q7MxgG5pUWzvv1tZuL3NJgSdAJY47/rUn/yw==`

**Installed *is* current upstream.** npm's published versions end at 0.4.7
(2026-04-03), so there is no version-to-version auth transition to
characterise — the published package is simply behind whatever the repository
documentation describes. No credential the owner can supply changes anything,
because nothing reads one.

Where the phantom came from: the Minara *skill* frontmatter declares
`"primaryEnv": "MINARA_API_KEY"` and its prerequisites state the variable
"bypasses login". The CLI it installs implements no such thing. A capability
was recorded from documentation rather than from the shipped artifact.

## 2. Actual authentication

Interactive `minara login` — device-code / email OAuth — writing
`~/.minara/credentials.json` (dir `0700`, file `0600`). Credential shape:

```ts
interface Credentials { accessToken: string; userId?; email?; displayName? }
```

Account-scoped bearer token; not wallet-scoped, not machine-scoped. On expiry
`auth-refresh.ts` prints *"Run `minara login` to manually refresh your session"*
and prompts. **No environment-variable path exists.** Neither `root` nor
`gecko-agent` has ever authenticated — no `~/.minara` exists on the host.

## 3. The capability Gecko needs is absent

`intent-to-swap-tx`: **0 hits** in both the package and the source.

The command surface is `account`, `assets`, `balance`, `chat`, `deposit`,
`discover`, `limit-order`, `login`, `logout`, `perps`, `premium`, `swap`,
`transfer`, `withdraw`. These operate on Minara-account wallets and are executed
server-side, returning a transaction *result*. `withdraw` is documented as
*"Withdraw tokens from your Minara wallet to an external address"* — it can send
**to** an address we name, but it does not construct a transaction **for** an
address we control.

**Scope note.** This record does NOT claim Minara is legally or
cryptographically custodial; who ultimately holds the private keys was not
established and was not needed. The proven and sufficient fact is narrower:
0.4.7 exposes Minara-account wallet operations and server-executed fund
movement, and exposes **no unsigned transaction for an external Gecko signer**.

This removes the architectural premise the lane was built on. BL-074 and the
2026-07-26 live-DEX verification recorded "gecko alerts → Minara executes" with
**broadcast path is OURS**. The shipped product cannot support that shape.

## 4. Why the ruling is UNSAFE, not merely "requires login"

`MINARA_AUTH_REQUIRES_INTERACTIVE_LOGIN` is literally true but insufficient,
because both interactive safeguards are inert on the Linux host:

- **Touch ID** — `if (platform() !== 'darwin') { ... }`; the source comments
  *"On non-macOS platforms a warning is shown and execution continues."* On
  Linux the gate is a **no-op**.
- **Transaction confirmation** — `if (config.confirmBeforeTransaction === false)
  return;`. Disableable from `~/.minara/config.json`.

So on the VPS a `credentials.json` plus one config flag yields **fully
non-interactive spend authority** over Minara-account wallets: swap, transfer,
withdraw to an arbitrary address, perps. That is the "unattended signing" and
"broad autonomous trading" shape the launch directive lists under **Never
enable** — and authenticating would still not deliver the unsigned-artifact
capability the lane requires.

**Therefore: do not create `~/.minara/credentials.json` on the VPS, do not fund
a Minara wallet, and do not attempt to make Minara's confirmation UX serve as
Gecko's authorization boundary.**

## 5. Milestone record — replaces the stale wording

```
The prior MINARA_API_KEY owner blocker is invalid and retired.

The supervised Solana on-chain objective was already achieved on 2026-08-01
through Gecko's Jupiter -> deterministic validation -> simulation -> exact
authorization -> local signing -> Jito path.

Minara itself is not an eligible Gecko execution path in current upstream
0.4.7 because the required unsigned external-wallet transaction capability
does not exist.
```

**Un-blocking condition — capability-based, deliberately not bound to a command
name** (so a future vendor implementation under any API satisfies it):

> Minara execution remains blocked until the vendor exposes a reviewed,
> runtime-proven mechanism that constructs an unsigned transaction for an
> arbitrary Gecko-controlled external address, without Minara signing or
> broadcasting it, and permits Gecko to independently validate, simulate,
> authorize, locally sign, broadcast through its approved path, and reconcile
> the result. Current `minara@0.4.7` exposes no such mechanism.

## 6. Vendor contact

**Not warranted.** Source, packaged artifact and registry metadata agree with no
material ambiguity. It becomes warranted only if Minara publishes past 0.4.7
with a capability meeting the condition above.

## 7. Disposition

Lane parked. No further Minara engineering warranted. The coherent EVM execution
path remains 0x — it keeps validation, signing and broadcast with us — and the
already-landed `ZeroExAllowanceHolderAdapter` has no quote-fetch client, which is
a real missing component rather than a configuration lever.
