**New primitives introduced:** `curve_launch_discoveries` table (curve-native
launch registry, two clocks + provenance), `curve_launch_events` append-only
evidence table (reorg-aware, raw-payload-preserving), `scout/ingestion/rh_pons.py`
(versioned Pons deployment registry + event-signature registry + log decoder +
lifecycle projector, inert by default), `RH_PONS_*` settings block,
`scripts/compare_discovery_latency.py` (read-only latency comparison harness),
`execution_eligibility()` unknown-is-ineligible mapper.

# Design delta — RH/Pons discovery increment (2026-09-13)

**Status:** APPROVED-BOUNDED implementation. Operator authorization recorded
2026-09-13 (session `claude/main-session-last-discussion-b8hsn7`); scope =
coverage matrix, additive inert RH Pons collector, evidence integration,
latency comparison harness, focused tests. Production deployment, activation,
external alerts, signing, funding, and live trading are explicitly OUTSIDE
this authorization.

This is a **delta on the accepted EVM-first design** (three independent
results — discovery evidence / opportunity rank / execution eligibility; two
clocks; exits in v0; four-chain coverage audit; immutable evidence; no
hindsight-filtered universe). It does not restart the architecture.

## Operator rulings encoded here

1. **`x1L` is unresolved.** No allowlist is claimed. Protocol-supported quote
   assets are recorded from first-party/onchain evidence only; protocol
   support ≠ operator approval to trade. Unresolved/unapproved quote assets ⇒
   `execution_eligible = 0` with explicit reasons; observations continue.
2. **Reuse evidence infrastructure with correct semantics.** Curve launches
   are NOT pools: they get their own `curve_launch_*` tables rather than being
   forced into `dex_pool_discoveries` (whose UNIQUE key and semantics are
   pool-addressed). The gt_new_pools lane discipline (flag-gated, observe-only,
   no CandidateToken emission, heartbeat, INSERT OR IGNORE dedup) is copied;
   the I1/I2/I3 observe-only spec structure is followed; delivered-alert events
   stay in their own ledger — never mixed with observations.
3. **Suppression boundaries are modeled separately** (see
   `tasks/findings_chain_coverage_matrix_2026_09_13.md` §3): scorer floor
   (`MIN_LIQUIDITY_USD` $15k, `chain=='coingecko'` exempt, score-rejection),
   DEX discovery floor (`DEX_DISCOVERY_MIN_LIQUIDITY_USD` $1k,
   discovery-exclusion), counter `liquidity_trap` hardcoded $15k/$30k
   (advisory RedFlag only). No threshold cleanup in this increment.
4. **Safety separation is a verified contract.** The legacy fail-open
   `is_safe()` remains alert-path-only (`scout/main.py:1344`). The execution
   path (`scout/live/`) was traced: it calls NO GoPlus function; its gating is
   capability-declaration + address-allowlist + mandate based. The new
   eligibility mapper reuses `is_safe_strict` semantics: `check_completed ==
   False` ⇒ `unknown` ⇒ ineligible. No legacy alert-behavior migration here.
5. **Historical findings seeded, not overstated.** Minara capability matrix
   (2026-08-01) and hermes-skills inventory are dated, provider-specific
   negatives; they are cited as such in the coverage matrix with
   configured/implemented/observed/verified reported separately.
6. **RH discovery now, inert by default.** Deployment metadata below is
   labeled by verification status; unknown deployments stay disabled; nothing
   is fabricated to make tests pass. Live polling refuses to run until the
   deployment registry entry is `onchain_verified` AND the flag is on AND an
   RPC URL is configured.

## Verified vs unresolved deployment facts (verification attempt 2026-09-13)

Network facts (multiple independent secondary sources, consistent):
Robinhood Chain mainnet, chain id **4663**, EVM (Arbitrum Nitro orbit), public
RPC `https://rpc.mainnet.chain.robinhood.com` (rate-limited, non-production),
explorer `robinhoodchain.blockscout.com`. First-party portal:
`docs.robinhood.com/chain`.

**Egress blocks recorded from this environment:** `docs.ponsfamily.com`
(first-party Pons docs), `docs.robinhood.com`, `docs.bitquery.io`, and the
chain RPC endpoint are all blocked by the network egress proxy
(`connect_rejected`). Therefore NOTHING below is first-party-confirmed or
onchain-confirmed from this session; every address/layout is
`source_derived_unverified` (source: `github.com/ponsmcp/pons-mcp` README +
`docs/PROTOCOL.md`, cross-read against Bitquery/Mobula/dev.to secondary
descriptions).

| Item | Value (source-derived) | Status |
|---|---|---|
| Pons V2 factory | `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e` | UNVERIFIED; **conflicts** with Bitquery-derived “active factory `0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB`, start block 8991118” |
| Launch router | `0xe33E9E479dF8802cb0866d5d05258bEc4cF62948` | UNVERIFIED |
| Uniswap v4 PoolManager (RH) | `0x8366a39cc670b4001a1121b8f6a443a643e40951` | UNVERIFIED |
| Meme hook on graduated pools | `0xe5e702641ea86f4ae6cc3cdaed2b886f976be044` | UNVERIFIED |
| V1 legacy factory (Uniswap v3 era) | `0x0c37a24F5D23A486FA692d1500881d698B1F77a4` (start block 8600612 per Bitquery) | UNVERIFIED |
| Curve quote asset | native ETH; `TokenLaunched.pairToken` carries the per-launch pair token | UNVERIFIED; x1L UNRESOLVED |
| Snipe tax | decaying, recipient-exemptible; **doc says 5s window, pons-mcp source-read says 3s hardcoded** | DISCREPANCY RECORDED |
| Phase machine | 0=NotGraduated, 1=Swept, 2=PoolCreated, 3=Rescued (README variant said 1/2/3) | DISCREPANCY RECORDED |
| Graduation | two-phase permissionless: `graduate()` sweep then `createGraduatedPool()`; separate txs; curve halts sells at `readyToGraduate()` | UNVERIFIED |

Event layouts (source-derived; topic0 derived at runtime by keccak-256 from
the canonical signature — derivation verified against the universal
`Transfer(address,address,uint256)` vector, so no hashes are hand-typed):

- `TokenLaunched(address indexed token, address indexed curve, address indexed deployer, address pairToken, uint256 launchConfigId, uint256 graduationThreshold)` — factory
- `PoolGraduated(address indexed token, uint256 positionId, uint256 tokenAmount, uint256 pairTokenAmount)` — factory
- `CurveBuy(address indexed buyer, address indexed recipient, uint256, uint256, uint256, uint256)` — per-launch curve
- `CurveSell(address indexed seller, address indexed recipient, uint256, uint256, uint256, uint256)` — per-launch curve
- `LaunchSwept(...)` — **layout UNKNOWN**: registered by name only, never
  decoded; unknown factory logs are preserved raw as
  `unknown_factory_event` so they can be re-decoded once the signature is
  resolved. Consequence: the `graduating` lifecycle state is currently only
  reachable via a later verified signal, and launches otherwise project
  `on_curve → on_v4` directly on `PoolGraduated` (transition-unknown handling,
  not an assumption that sweeps don't exist).
- `GraduationTokensPermanentlyLocked(...)` — layout UNKNOWN, same treatment.
- `Initialize(...)` on the v4 PoolManager: correlation target; decoding
  deferred until the PoolManager deployment is verified (non-standard fork
  risk was explicitly flagged by the source: “Uniswap router is non-standard
  fork”).

## Data model (smallest additive structure; justification)

`dex_pool_discoveries` is pool-addressed (UNIQUE(network, pool_address)) and
carries pool-shaped columns; a pre-pool curve launch has no pool address and a
distinct lifecycle, so forcing it in would fabricate identity. Two new tables:

1. `curve_launch_discoveries` — one row per (chain_id, token_address):
   canonical identity (token, curve, deployer, pair_token — curve discovered
   dynamically from `TokenLaunched.curve`), lifecycle projection
   (`on_curve | graduating | on_v4 | rescued | unknown`), two clocks
   (`event_time` nullable = unavailable-is-explicit; `first_seen_at` NOT NULL),
   provenance (`source`, `provenance`, `deployment_version`), and the
   execution-eligibility stamp (`execution_eligible` INTEGER default 0 +
   `eligibility_reasons` JSON). Mutable PROJECTION row (like `candidates`).
2. `curve_launch_events` — strictly append-only evidence: chain/block/tx/log
   identity, `block_hash` for reorg detection, `event_time` (block time when
   known, else NULL), `observed_at`, `provider_available_at` (nullable),
   source/provenance/deployment_version, `payload_json` raw-first. Dedup =
   `UNIQUE(chain_id, block_hash, transaction_hash, log_index)` + INSERT OR
   IGNORE. Reorgs never UPDATE evidence: a `removed:true` log or a
   same-(tx,log_index)-different-block_hash re-observation appends a
   `reorg_removed` / `reorg_replaced` marker row referencing the affected
   identity. Alert-delivery events remain in their own ledger.

Existing two-clock columns elsewhere are treated as precedent, not as proof:
each new feature here carries its own point-in-time provenance fields.

## Collector shape

`scout/ingestion/rh_pons.py`, mirroring `gt_new_pools.py` guardrails: emits NO
`CandidateToken`; nothing reaches aggregate()/scorer/gate/alerts; gated by
`RH_PONS_COLLECTOR_ENABLED` (default False — pipeline byte-identical when
off); never raises into `run_cycle`. Core is transport-free:
`collect_from_logs(raw_logs, db, settings, *, source, provenance)` ingests
already-fetched log dicts (fixtures today, RPC client later), so decode,
first-buy-in-launch-transaction ordering, duplicates, reorg markers, and
lifecycle projection are all testable without network. `poll_once()` refuses
(with a structured reason) unless flag ON + RPC URL configured + deployment
`onchain_verified` — all three are false today, so the collector is inert
three times over. Backfill/reconnect resume from
`MAX(block_number)` over canonical evidence rows.

## Latency comparison harness

`scripts/compare_discovery_latency.py` (read-only): for each curve launch,
event-time → RH-collector `first_seen_at` latency, and cross-lane comparison
against `candidates.first_seen_at` (CG/DS/GT) and
`dex_pool_discoveries.first_seen_at` by contract identity. Censoring is
explicit: `event_time_unavailable`, `never_observed_cg_ds_gt`,
`never_observed_dex_lane` are reported as censored categories, never as zero
latency. Provider-availability time is reported where known, else
`unknown`. Output: structured JSON.

## Unresolved dependencies (exact)

1. First-party confirmation of factory/router/PoolManager/hook addresses +
   deploy blocks (docs.ponsfamily.com / docs.robinhood.com egress-blocked;
   operator can supply, or run verification from a network-capable host).
2. Onchain verification (RPC egress-blocked): `eth_getCode` on registry
   addresses; one real `TokenLaunched` log to confirm derived topic0s; block
   timestamps for event_time.
3. `LaunchSwept` / `GraduationTokensPermanentlyLocked` layouts; v4 fork
   `Initialize` layout on the RH PoolManager.
4. `x1L` definition and the operator-approved quote-asset allowlist.
5. Resolution of the factory-address conflict (`0x7eD5…` vs `0xA5aA…` — the
   latter may be a newer redeploy; both are recorded in the registry, both
   disabled).
