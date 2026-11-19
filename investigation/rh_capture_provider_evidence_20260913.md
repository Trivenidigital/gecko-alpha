# RH capture provider evidence — 2026-09-13

These are bounded read-only observations against the public Robinhood Chain
RPC. The sustained smoke used an isolated temporary database, with execution
eligibility disabled. It did not change production configuration or records.

`rh_rpc_capacity_probe_20260913.json` records transport capability samples:
JSON-RPC header batches and topic-only log queries were accepted. This does
not establish sustained capacity or a provider quota.

`rh_capacity_smoke_throttled_20260913.json` records the subsequent failed
capture smoke at candidate `d4694a06e8ab0e6c17eaeb1d66da6cdd57bea66d`:
seven passes, four completed, three rate-limited, 420 events and 26 launches.
The initial 3,000-block backlog did not drain. No steady-state latency claim
is supported. The report predates review fixes to the probe's verdict and
checkpoint verification; retain it as raw evidence, not a passing certificate.
The `rpc_calls` count measures logical JSON-RPC methods, not HTTP POSTs.

Official documentation checked on 2026-09-13:

- [Connecting to Robinhood Chain](https://docs.robinhood.com/chain/connecting/)
  describes the public RPC as rate-limited and recommends a provider for
  production. It lists Alchemy, QuickNode, Blockdaemon, dRPC and Validation Cloud.
- [Robinhood Chain terms](https://docs.robinhood.com/chain/terms-of-service/)
  explicitly excludes production-grade, high-throughput and latency-sensitive
  workloads from the intended public RPC use.

No numeric quota was confirmed. The observed burst failures do not establish
one. Further public load testing was paused. Local pacing and integrity fixes
can proceed, but production readiness requires suitable provider capacity and
a successful bounded capture assessment using the corrected probe.
