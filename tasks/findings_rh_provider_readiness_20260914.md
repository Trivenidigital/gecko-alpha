# RH provider readiness — 2026-09-14

## Current state

PR576 is merged as `e6a55d7ad3f2029d3ef63fea0104d49a9e082fca`.
Its final CI passed 7,931 tests plus 118 dashboard contract checks.
The read-only production check on 2026-09-14 found `gecko-pipeline` active,
but no configured `RH_PONS_RPC_URL`, `ROBINHOOD_RPC_URL` or `ALCHEMY_API_KEY`
in the project dotenv/service environment. The RH collector is not enabled.
The local project dotenv check also found none of those configured keys.
No key values were printed and no production state was changed.

## Provider constraints checked

| Source | Observed documentation | Consequence |
|---|---|---|
| [Robinhood connection guide](https://docs.robinhood.com/chain/connecting/) | Public RPC is rate-limited and not recommended for production; Alchemy and other providers are listed | Do not repeat sustained public-endpoint load after the failed smoke |
| [Alchemy RH getLogs](https://www.alchemy.com/docs/chains/robinhood-chain/robinhood-chain-api-endpoints/eth-get-logs) | Free tier permits 10 blocks per query; PAYG lists unlimited block range for Robinhood mainnet, with a 150 MB response cap | Free-tier credentials alone do not support the collector's current scan and overlap settings |
| [Alchemy throughput](https://www.alchemy.com/docs/reference/throughput) | Limits apply across an account and use a rolling 10-second window | Verify actual account limits and competing app usage before assessment |
| [Alchemy compute-unit costs](https://www.alchemy.com/docs/reference/compute-unit-costs) | Block-header reads cost 20 CU, log reads 60 CU; batches sum their constituent methods | HTTP batching does not remove metered method costs |

These are documented limits, not verified runtime entitlements for a user
account. No account has been selected, no subscription purchased, and no
authenticated RPC request has been made in this follow-up.

## Next evidence required

1. Identify the provider/account and its available Robinhood mainnet endpoint
   through a secret-safe local configuration path.
2. Verify chain 4663, supported log ranges, batch behavior and actual rate limits
   with a small preflight, without activating the production collector.
3. Run the corrected bounded capacity assessment in an isolated evidence DB;
   retain backlog-drain, steady-state delay, failure and checkpoint results.
4. Do not infer useful early-alert performance or execution readiness from
   transport success alone; those remain separate measured gates.

Claude Max is performing the independent credential/compatibility audit.
Provider account information has been requested from the operator. Coding
changes, if needed, must address concrete gaps found by that audit.
