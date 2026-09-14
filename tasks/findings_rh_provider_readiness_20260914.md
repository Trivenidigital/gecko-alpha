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

## Probe readiness fixes (2026-09-14, branch codex/rh-provider-readiness)

The audit found probe-side blockers only; the collector's own logs already
omit the URL. The fixes change only the investigation CLI, its tests and the
runbook. Collector, config, DB and schema are untouched.

| Gap | Design | Pinned by |
|---|---|---|
| Keyed URL only accepted on the command line | `--rpc-url-env NAME` reads only that variable, in a mutually exclusive group with `--rpc-url` (public default kept). Settings stay init-only. A missing, empty or malformed value (scheme, host or port) raises `ProbeConfigError ... from None` before traffic, naming the variable without its value; `main` exits 2 | env, exclusion, 7 refusal cases, `main` stderr, end-to-end report without the secret |
| Report `endpoint` redacted `args.rpc_url` (found while implementing) | Redact the URL actually used; `isolation` lists variable names read, replacing the untrue `reads_dotenv_or_environment: false` | end-to-end report test |
| Provider range/batch limits unchecked | `--provider-log-range-cap` refuses `max_span + RH_PONS_REORG_OVERLAP_BLOCKS` above the cap (a checkpointed pass queries `next - overlap .. next + span - 1`). `--provider-batch-cap` refuses a header batch above its cap. Both run in `run()` before the DB-output check and the session | 2012/2011 boundary, Alchemy free 10 vs 13, batch 50/49, refusal before traffic |
| Failing provider kept using budget | `--stop-after-failed-passes N` (opt-in, positive): the longest consecutive failed/timeout/error/refused streak reaching N gives `stop_reason=failed_passes` and failure `stopped_on_failed_passes`. It is checked after the rate-limit stop, which keeps priority | streak and reset cases, opt-in default, priority, fail verdict despite good evidence |
| Probe used the proxy environment | `ClientSession(trust_env=False)`, like the pipeline | session kwargs test |
| Redaction only tested with a path key | userinfo, path, query and fragment tests; hostname kept (a key in a subdomain is a residual, documented) | 5 cases |

Deliberately deferred (out of scope): the error-code delta in the collector's
invalid-response log, per-method or compute-unit counters, a `batch_headers`
transport field, paid-provider integration and `SecretStr` for
`RH_PONS_RPC_URL`. The existing call cap, rate-limit stop and new failure stop
bound diagnostics for now.

The Alchemy free tier is incompatible with the current configuration: with the
reorg overlap of 12 (fixed in the probe, collector default), the smallest
checkpointed query is 13 blocks, above its 10-block cap. The probe refuses it
before traffic when given `--provider-log-range-cap 10`.

### Independent ops-safety review follow-up (2026-09-14)

- F1 (fixed, docs only): the runbook's authenticated invocation was one
  copy-paste block, so pasting it let `read` consume the probe line and the
  key pasted afterwards would run as a command and enter shell history. The
  secret read (`IFS= read -rsp`, run alone), the probe invocation and `unset`
  are now separate steps.
- N2 (fixed, docs only): the free-tier incompatibility is stated for the
  current overlap of 12, not as absolute.
- Non-blocking residuals, no code change:
  - N1: out-of-range CLI values raise a Pydantic `ValidationError` from
    `build_settings` (exit 1 with a traceback) instead of the documented exit
    2 refusal. It happens before traffic and field errors carry only that
    field's input, not the URL.
  - N3 (collector-side, existing): an exception escaping the per-pass guard
    (for example in `_record_attempt` or `_finish_pass`) ends the worker task.
    The probe then waits `--max-seconds` and fails loudly at `await task`
    without a report. That is a crash, never a false pass.

### Verification record

- Tests first (commit `a5f2149a`), Codex native run: 27 failed / 30 passed. The
  15 pre-existing tests stayed green; the redaction cases and argparse-only
  rejections passed before the change, as predicted.
- Implementation `deee7772`, Codex native run: probe and RPC-check suites,
  74 passed. The local stubbed-aiohttp smoke caught a missing `from None` on
  the empty-variable refusal before the native run.
- Guard mutants (12: URL validity, port, overlap in the range check, batch
  cap, positivity boundary, streak memory, failure verdict, `trust_env`,
  mutual exclusion, preflight call, report endpoint source, `from None`),
  Codex native run: all 12 killed with no survivors or skips. Source was
  restored byte-identical; 74 passed afterwards.
- Independent review and exact-head CI: pending. Nothing was integrated.
- No network, production, merge or activation actions were taken.
