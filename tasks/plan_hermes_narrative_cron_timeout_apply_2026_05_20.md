**New primitives introduced:** NONE (this PR ships VPS-side parallelization of `narrative_classifier` calls inside `/home/gecko-agent/run-scanner-cycle.py` via `concurrent.futures.ThreadPoolExecutor`; docs-only repo PR with plan + design + runbook + backlog status flip).

# Plan: BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY

## Background — what PR #201 left us with

PR #201 (commit `061bfd8`) shipped Step 1 instrumentation. First instrumented
cycle (2026-05-20T04:00:53Z) showed:

| Stage | Duration | % of cycle |
|---|---|---|
| kol-watcher | 12.11s | 5.2% |
| **narrative-classifier** | **222.14s** | **94.8%** |
| coin-resolver | 0.00s | 0% (no CAs this cycle) |
| narrative-alert-dispatcher | 0.02s | <0.1% |
| **Total** | **234.28s** | — |

Per-tweet OpenRouter call latency at the 04:00 cycle: ~11s per tweet × 20 tweets = ~222s.
Earlier cycles (pre-instrumentation): 27 tweets × ~4s = ~108s for narrative-classifier.

Bottleneck is unambiguous: the **classifier loop is the only stage that scales with tweet volume + has per-tweet OpenRouter network latency**.

## Drift-check (§7a per AGENTS.md)

**Deployed file:** `/home/gecko-agent/run-scanner-cycle.py` (commit-equivalent
`3af48d9`-deployed). Verified at SSH-probe time — script unchanged since the
PR #201 instrumentation deploy. No drift.

**Backlog:** `BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY` filed in PR #201's `backlog.md`.

**Cron jobs.json:** `gecko-x-narrative-scanner` still `no_agent: true`,
`schedule: 0 * * * *`, `enabled: true`. No drift.

**Wrapper script:** `/home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh`
unchanged since PR #201 (umask 0027 + chmod 0640). No drift.

## Hermes-first check (§7b per AGENTS.md)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Concurrent HTTP client | None applicable (Hermes optional-skills catalog has no Python-side concurrent-classification helper) | Build in-project; use stdlib `concurrent.futures.ThreadPoolExecutor` |
| LLM batching / fan-out helpers | None applicable | Build in-project |
| OpenRouter rate-limit helper | None applicable | Use defensive concurrency=5 to stay well under OpenRouter's ~200 req/min ceiling for free tier |
| Awesome-hermes-agent ecosystem check | No applicable skill | Build in-project |

**Verdict:** stdlib only. No Hermes-ecosystem code applies.

## Root-cause hypothesis (now PROVEN)

`classify_tweets()` at `/home/gecko-agent/run-scanner-cycle.py:295-509` runs a
serial for-loop that:

1. For each tweet in `new_tweets`:
2. Build prompt (CPU-cheap)
3. **`requests.post()` to OpenRouter with `timeout=30`** — this is the per-tweet latency that adds up
4. Parse JSON response
5. Run `verify_hard_extraction_invariant` (CPU-cheap)
6. Increment `state.*` counters
7. Append to `classified_events`

Steps 1, 2, 4, 5, 6, 7 are CPU-bound and fast (<10ms each).
Step 3 is IO-bound, ~4-11s per tweet, sequential.

**Sequential pattern is the bottleneck.** Parallelizing step 3 across multiple
tweets (`concurrent.futures.ThreadPoolExecutor`) compresses N tweets × ~7s into
ceil(N/concurrency) batches × ~7s.

## Operator-pinned fix-shape constraints

Per operator's 6-hour assignment:

1. **Preferred:** bounded parallel classification with explicit concurrency limit.
2. **Acceptable:** per-cycle classification cap + carry-forward queue, if parallelism is too risky.
3. **Last resort:** raise Hermes timeout, but only if lifecycle/orphan semantics are fixed or explicitly documented as separate blocker.

Evidence strongly supports option 1. The classifier IS structurally parallelizable;
OpenRouter rate limits accommodate concurrency=5 with headroom; the existing
hard_extraction_invariant + confidence_floor logic operates on per-tweet outputs
that don't need order-dependency.

## Plan steps (in order)

### Step 1 — Pick concurrency limit (pre-registered)

Three candidate concurrency values evaluated:

| Concurrency | Max tweets/cycle under 100s budget | Per-tweet OpenRouter pressure | Verdict |
|---|---|---|---|
| 1 (current) | ~14 (at 7s/tweet) | 1 in-flight | Insufficient — 20+ tweets common |
| 5 | ~70 (at 7s/tweet × 5 = 35s budget) | 5 in-flight × ~0.7 req/sec each | **RECOMMENDED — comfortable headroom + safe rate-limit** |
| 10 | ~140 | 10 in-flight × ~0.7 req/sec | Riskier rate-limit; not needed yet |

**Recommendation: concurrency = 5.** Adjustable via constant if profiling shows tightness.

### Step 2 — Thread-safety design

`state.*` counters (`state.skips`, `state.openrouter_4xx`, `state.openrouter_5xx`,
`state.classification_other_error`, `state.speculative_cas_scrubbed`,
`state.duplicates`, `state.new_tweets`) are mutated inside the classifier loop.

**Three options:**

A. **`threading.Lock` around each increment** — verbose, ~7 lock acquisitions per
tweet. Safe but adds latency.

B. **Aggregate-after-gather** — each thread builds a LOCAL `LocalCycleState`
dict, then merge into module-level `state` after ThreadPoolExecutor finishes.
Clean, lock-free, mainstream.

C. **Use `atomic` counters from `collections.Counter` or `multiprocessing.Value`**
— overkill for this use case.

**Recommendation: Option B.** Each future returns a `(classified_event_or_None, local_state_delta)` tuple; main thread aggregates after `concurrent.futures.as_completed()` iteration.

### Step 3 — Hard-extraction-invariant ordering preservation

`verify_hard_extraction_invariant(classification, tweet_text)` is per-tweet and
order-independent — it only inspects the classifier output against the tweet
text. Safe to parallelize. Verified by inspection of the function at
`run-scanner-cycle.py:142-178`.

### Step 4 — Order preservation for downstream consumers

`classified_events` list order matters for the dispatcher's `event_id`
computation? Let me trace: `event_id = sha256(tweet_id | text_hash | ca | cashtag)`.
The hash is computed PER ITEM, not per cycle — so item ordering inside the
`classified_events` list does NOT affect downstream behavior.

**Conclusion:** safe to reorder. We'll use `as_completed()` for natural ordering
by completion time. If strict input-order is preferred for log readability, use
`executor.map(...)` instead (slightly less efficient but order-preserving).

**Recommendation:** use `as_completed()` — log lines naturally interleave by
completion time, which gives the operator a real-time sense of which tweets are
slow vs fast.

### Step 5 — Per-tweet timing instrumentation (additive)

Add a per-tweet inner-timing log: each future emits its own start + completion time,
contributing to the cycle's narrative-classifier stage telemetry. Not strictly
needed for the fix but useful for future profiling. **Defer** — keep this PR
scope tight; file as follow-up.

### Step 6 — Fall-back behavior

If `ThreadPoolExecutor` itself raises (rare; e.g., resource exhaustion), the
existing top-level `try` block in `main()` catches and emits the
`SCANNER-CYCLE-SUMMARY` with the partial state.

If a single future raises an unhandled exception, `future.result()` re-raises;
we catch and increment `state.classification_other_error`.

If OpenRouter rate-limits us mid-parallel-batch (HTTP 429), we get a burst of
4xx counter increments; the cycle continues with remaining tweets. The `skips`
counter accumulates and the cycle still emits a clean SUMMARY.

## Safety invariants (MUST hold)

1. **`no_agent: true` cron mode unchanged.**
2. **No secret exposure.** No new `log()` interpolation of `os.environ`, response.text/.headers, HMAC payload, bearer tokens.
3. **No classifier-threshold change.** 0.6 confidence floor preserved.
4. **No `os.popen("curl -H ...")` patterns.** Same as PR #201 invariants.
5. **`event_id` idempotency semantics unchanged.** No dispatcher-side change.
6. **No DDL / database changes.** Pure Python script edit.
7. **Existing SCANNER-* instrumentation preserved.** Per-stage timings continue to emit.
8. **`hard_extraction_invariant` per-tweet check preserved.**
9. **`narrative_alerts_inbound` write semantics unchanged** (dispatcher unchanged).
10. **No regression for low-volume cycles** — concurrency=5 with <5 tweets just runs them in parallel; identical behavior to sequential for n=1.

## Acceptance criteria (pre-registered)

For the build (Step 1.7) + post-deploy verification (Step 1.8):

1. **At least one cycle completes in <120s** with a representative classifier workload (20-30 tweets). Evidence: `SCANNER-CYCLE-SUMMARY` line shows `duration-sec < 120.0`.
2. **`jobs.json` `last_status` flips to `"success"`** for the cycle in (1).
3. **All 4 stages emit START + TIMING pairs** (no SIGKILL'd cycle expected; the OpenRouter calls don't take longer in parallel than they did in serial — they just overlap).
4. **`narrative-classifier` stage duration** drops from ~222s to under ~50s (concurrency=5 with 20 tweets at ~10s each = 4 batches × 10s = ~40s).
5. **`SCANNER-OPENROUTER-ERROR` rate** stays close to zero — concurrency=5 must not trigger OpenRouter rate-limiting.
6. **`alerts-dispatched` count and `tweets-inspected` count are within expected ranges** — parallelization must not change behavior, only speed.
7. **No prompt-injection regression.** Journal grep clean.
8. **No secret leakage.** Cycle-report log grep clean.

## Out-of-scope (deferred)

- **Per-cycle classification cap with carry-forward queue.** Option 2 from operator's preference list. Only needed if option 1 (parallelize) proves insufficient under sustained high volume. Filed as a fallback if needed.
- **Timeout extension (HERMES_CRON_SCRIPT_TIMEOUT).** Option 3 (last resort). NOT shipped here — operator's constraint explicit.
- **No-agent-flag watchdog** (P2 in this 6-hour block) — separate PR.
- **Subprocess-lifecycle audit** (P3) — separate audit.
- **Deferred-resolution-sweep** — gated on cron stability first.

## Rollback

If the parallelized cycle behaves unexpectedly:

1. SSH to srilu-vps as root.
2. Atomic `mv` the timestamped backup `run-scanner-cycle.py.bak.<gitsha>-<unixtime>` back to `/home/gecko-agent/run-scanner-cycle.py`.
3. Set mode back to 0664 + chown gecko-agent.
4. Next cron tick reverts to the pre-fix sequential behavior.

Total reversion: <1 minute.

## Pre-build reviewer focus (P1.2)

Two parallel vectors against this plan:

- **Vector A (Runtime/concurrency safety):** races, duplicate dispatch, queue/idempotency,
  thread-safety on `state.*` counters, OpenRouter rate-limit behavior under concurrency=5,
  exception propagation from threads, hard_extraction_invariant ordering, ThreadPoolExecutor
  cleanup on cron SIGTERM, behavior in low-volume cycles (n=0,1,2 tweets).
- **Vector B (Security/prompt/secret safety):** any new log surfaces under concurrent calls,
  potential for response.text/.headers/auth-header to land in stdout under exception
  propagation, no-agent regression risk, OpenRouter key handling unchanged.

## Open questions for reviewers

1. Is concurrency=5 the right starting value, or should we begin at 3 (safer) and tune up?
2. Should `as_completed()` ordering be replaced with `executor.map()` for stable log order — even at slight efficiency cost?
3. Should we add a per-tweet timing breakdown (Step 5) in this PR or defer?
4. Is the LOCAL_STATE aggregate-after-gather pattern (Option B) right, or should we use a lock?
5. What's the right behavior if a SINGLE future raises mid-batch — abort the rest of the cycle, or continue with remaining tweets and emit cycle summary?
