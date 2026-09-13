# RH / Pons public deployment verification — 2026-09-13

The current `pons_v2` factory `0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e` has sufficient public evidence to identify it as the deployed curve V2 factory and to promote its **observation registry identity** after review. No registry, collector flag, runtime configuration, or trading eligibility was changed in this investigation.

The alternate `0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb` is a different contract family: Blockscout returns `PonsLaunchFactory`, whose source describes direct fixed-supply launches into one-sided Uniswap V3. Its TokenLaunched event has ten parameters, versus six for curve V2. The registry's suggestion that it may be a newer V2 redeployment is unsupported and should be corrected. Both addresses have nonempty deployed code; zero logs in one bounded window does not prove the alternate inactive forever.

## Reproducible evidence

Compact responses, source-derived event ABI, bytecode hashes, response hashes, and raw example launch/graduation logs are saved in [public evidence](../investigation/rh_pons_public_evidence_20260913.json). Queries used public documented endpoints, without credentials, transactions, challenge bypass, private files, or region workarounds.

- [Robinhood connecting documentation](https://docs.robinhood.com/chain/connecting/) confirms mainnet chain ID **4663**, names the public RPC, and links the explorer. It explicitly warns the public endpoint is rate-limited and unsuitable for production.
- [V2 verified source / ABI](https://robinhoodchain.blockscout.com/api/v2/smart-contracts/0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e): `PonsV2LaunchFactory`, verified 2026-08-04. Its complete deployed bytecode exactly matched `eth_getCode` from the official public RPC. SHA-256 fingerprints and byte lengths are in the evidence.
- [Deployment transaction](https://robinhoodchain.blockscout.com/api/v2/transactions/0x3817f297aa7c2ef78789bffac57491ceedc218fef962d47ed36c272699deddeb): successful creation of the same address, block **26841846**, timestamp **2026-08-03T14:41:19Z**.
- [Alternate source / ABI](https://robinhoodchain.blockscout.com/api?module=contract&action=getsourcecode&address=0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb): `PonsLaunchFactory`, direct V3 pool launch mechanism. This establishes different ABI/mechanism; it does not establish deprecation or a complete version chronology.

## Actual event evidence

Two bounded 10,000-block V2 factory queries returned fewer than the usual 1,000-result cap. These are retrospective reads, **not measured first-observation latency or demonstrated early detections**.

| Block interval | Event timestamps UTC | Launches | Graduations | Total logs |
|---|---|---:|---:|---:|
| 62106833–62116832 | 2026-09-13 16:42:22–16:59:10 | 319 | 4 | 412 |
| 62172353–62182352 | 2026-09-13 18:33:15–18:50:11 | 327 | 12 | 428 |

The later interval ends at the RPC head observed during this investigation, `0x3b4d3d0`. The alternate factory returned zero logs for the same later interval. Dividing launch counts by first-to-last event spans gives approximately **1,139–1,159 launches/hour** in these short samples. This is a local activity estimate, not a daily forecast, genuine demand estimate, or forecast of useful/tradable signals.

The raw examples preserve actual emitter, topics, data, transaction hash, block number, log index, and explorer timestamp. An actual V2 launch emits from the factory; the event points to a distinct curve address. One curve's bounded logs showed initialization/ownership events, confirming the separate emitter address. A second curve selected from a launched-and-graduated token timed out on the explorer query, so **no actual CurveBuy/CurveSell example was obtained** in this bounded investigation.

## ABI conclusions

The verified V2 ABI confirms:

- `TokenLaunched(address,address,address,address,uint256,uint256)` with indexed token, curve, deployer and non-indexed pairToken, launchConfigId, graduationThreshold.
- `PoolGraduated(address,uint256,uint256,uint256)` with indexed token and non-indexed positionId, tokenAmount, pairTokenAmount.
- The verified compiler source bundle includes `PonsV2BondingCurve.sol`, whose CurveBuy/CurveSell declarations match the collector's six-argument signatures, with buyer/seller and recipient indexed. This is verified source evidence, not an observed trade-emitter bytecode comparison.
- LaunchSwept, LaunchGraduationRescued, and GraduationTokensPermanentlyLocked layouts are now available in the retained factory ABI. Their presence does not by itself establish correct lifecycle handling by the collector.

The alternative factory has `TokenLaunched(address,address,address,address,address,uint256,uint256,uint256,uint256,uint256)` and lacks the same curve-V2 meaning. Do not decode its logs with the V2 layout.

## Operational residuals before useful live discovery

1. Registry promotion can be limited to verified V2 factory identity, deployment block and sources; it must not imply quote approval, safety eligibility or live-trading authorization. Auxiliary router/hook/pool-manager addresses were not independently verified here.
2. Production RPC transport still needs an authorized working endpoint. Ordinary single eth_getCode/blockNumber requests worked, but eth_getLogs and receipt requests returned HTTP 403. Batch requests received a Cloudflare challenge. Blockscout v2 endpoints intermittently returned 500, while normal legacy public source/log endpoints worked. No barrier was bypassed.
3. Obtain a real curve-trade fixture and verify curve code identity before claiming end-to-end trade collection against production. Existing compiled-source ABI evidence is stronger than the prior third-party description but cannot replace runtime proof.
4. The high local launch rate supports a short data-bound observation gate, conditional on provider capacity, complete cursor traversal and watchdog health. The original early-signal goal still requires live observation timestamps and a comparison against baseline discovery; historical event timestamps must not be used as historical first-observation times.

Only public investigation artifacts changed. No transactions, activation or deployment occurred.
