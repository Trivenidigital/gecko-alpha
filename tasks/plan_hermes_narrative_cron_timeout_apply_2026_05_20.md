**New primitives introduced:** NONE (this PR ships VPS-side parallelization of `narrative_classifier` calls inside `/home/gecko-agent/run-scanner-cycle.py` via `concurrent.futures.ThreadPoolExecutor` with `threading.Lock`-protected state.* increments + `fcntl.flock` overlapping-cycle guard + per-worker exponential-backoff on 429; docs-only repo PR with plan + design + runbook + backlog status flip).

# Plan v2: BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY

## Plan-review fold log (2026-05-20)

Two reviewer vectors returned **3 Critical + 5 Important findings**. All folded into v2 below.

| Finding | Vector | Status |
|---|---|---|
| C1: concurrency=5 = ~43 req/min steady-state likely exceeds OpenRouter free-tier 20-60 req/min ceiling. Cascading 429 risk. | A | FOLDED — concurrency=**3** for first deploy (~26 req/min), promote only after ≥5 clean cycles + confirmed OpenRouter tier |
| C2: ThreadPoolExecutor SIGTERM aggregation race — partial state at summary emit | A | FOLDED — switch from "aggregate-after-gather" to `threading.Lock`-protected in-future increments. `cancel_futures=True` on shutdown. |
| C3: 5-way 429 cascade — all workers fail in burst, cycle reports clean but classifier output collapses | A | FOLDED — per-worker exponential backoff on 429 (2s/4s/8s, 3 retries); state counter `state.openrouter_429_burst_count` |
| I1: state.new_tweets is mutated in kol_watcher (single-threaded), NOT classify_tweets — remove from "mutated under concurrency" list | A | FOLDED — clarified in §Step-2 below |
| I2: Build-stage must include literal VPS-read of classify_tweets / verify_hard_extraction_invariant / build_event_id | A | FOLDED — added to Build deliverables |
| I3: Overlapping-cycle risk — fcntl.flock guard at script entry | A | FOLDED — new §Step-1.5 |
| I4: as_completed() exception semantics — pin explicit "continue, not abort" | A | FOLDED — §Step-4 |
| I5: requests.Session reuse for connection pooling — defer | A | FOLDED — out-of-scope (filed as follow-up `BL-NEW-SCANNER-REQUESTS-SESSION-POOL`) |
| B-I1: pre-existing `os.popen("xurl ...{handle}")` at :287/:305 — out of scope but acknowledge | B | FOLDED — §Out-of-scope note added |
| B-M1: replace bare `traceback.print_exc()` with `traceback.format_exception_only(...)` for future-safety | B | FOLDED — added to Hunk 1.4 area |
| B-M2: enumerate the 6 worker log call sites + lock log surface | B | FOLDED — Invariant 11 below |

**Plan blockers resolved.** Ready for stage P1.4 (design) after one more clarification: the design-stage 2-vector review will verify the concurrency=3 choice + the threading.Lock implementation + the fcntl.flock against the live code.

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

### Step 1 — Pick concurrency limit (pre-registered, post-fold)

Three candidate concurrency values evaluated with Vector A's corrected
rate-limit math:

| Concurrency | Max tweets / 60s classifier budget | Steady-state req/min | OpenRouter compatibility | Verdict |
|---|---|---|---|---|
| 1 (current) | ~8 (at 7s/tweet) | ~8.6 req/min | Safe but useless | Insufficient |
| **3** | **~26 (3 × 60s/7s = 25.7)** | **~26 req/min** | **Comfortable under 60/min funded tier; tight against 20/min free tier — acceptable with backoff** | **RECOMMENDED for first deploy** |
| 5 | ~43 | ~43 req/min | Risky — likely exceeds free-tier 20/min; possibly exceeds funded 60/min sustained | Promote-target only after ≥5 clean cycles @ concurrency=3 + tier confirmation |
| 10 | ~85 | ~85 req/min | Almost certainly rate-limit exceeded | Out of scope |

**Recommendation: concurrency = 3** for first deploy. Constant `CLASSIFIER_CONCURRENCY = 3`. Promote to 5 only after evidence-gated re-deploy.

### Step 1.5 — fcntl.flock overlapping-cycle guard (Vector A I3 fold)

If a cycle orphans past 60min (e.g., a cluster of slow tweets pushes one cycle to 70 min), the next cron tick fires while the orphan still runs. Two simultaneous Python processes = 2× concurrency = 6 in-flight workers, doubling rate-limit pressure.

Mitigation: take an `fcntl.flock(LOCK_EX | LOCK_NB)` on `/home/gecko-agent/.hermes/cron/gecko-x-narrative-scanner.lock` at script entry. If held by a still-running orphan, log `SCANNER-CYCLE-SKIP-OVERLAP` and exit clean (status=0). Hermes cron will retry next hour. No queue buildup; the locked-out cycle simply doesn't run.

Lock file path: `/home/gecko-agent/.hermes/cron/gecko-x-narrative-scanner.lock`. Lock-fd held for the lifetime of the script (released on process exit; kernel-enforced).

### Step 2 — Thread-safety design (Vector A C2 + I1 fold)

`state.*` counters mutated inside the classifier loop:
- `state.skips`
- `state.openrouter_4xx`
- `state.openrouter_5xx`
- `state.classification_other_error`
- `state.speculative_cas_scrubbed`
- `state.duplicates`
- (NEW) `state.openrouter_429_burst_count` — per Vector A C3 fold

**Vector A I1 clarification:** `state.new_tweets` is appended in
`run_kol_watcher()` (single-threaded stage that runs BEFORE classify_tweets).
The classifier consumes it as a frozen list. NOT in the mutation list above.

**Decision (post-fold): Option A — `threading.Lock`-protected in-future
increments.** Vector A C2 found that "aggregate-after-gather" (Option B in
v1) creates a partial-aggregation race when SIGTERM hits mid-aggregation: the
SCANNER-CYCLE-SUMMARY emit then under-counts vs the dispatcher's actual TG
ground truth.

Concrete shape:

```python
_state_lock = threading.Lock()

def _classify_one(tweet):
    """Worker: one classifier call. Mutates module-level state via
    threading.Lock-protected increment block. Returns the classified_event
    or None."""
    # ... (POST to OpenRouter, parse, verify_hard_extraction_invariant) ...
    # Increment counters atomically (one critical section per tweet,
    # holding the lock for ~50µs of int increments):
    with _state_lock:
        if response.status_code != 200:
            if 400 <= response.status_code < 500:
                state.openrouter_4xx += 1
            elif response.status_code >= 500:
                state.openrouter_5xx += 1
            else:
                state.classification_other_error += 1
            state.skips += 1
        elif scrubbed:
            state.speculative_cas_scrubbed += scrubbed
    # outside lock — append/sort don't need atomicity for our use
    return event_or_none
```

This means: SIGTERM at any point leaves `state.*` consistent with what the
workers have ACTUALLY observed. The next operation is either a fresh
increment under the lock (atomic) or a final summary read (which reflects
the most-recent committed state). No partial-aggregation race.

Cost: one lock acquisition per tweet (~50µs). At 30 tweets/cycle = 1.5ms of
locking overhead. Negligible vs the 7s/tweet OpenRouter latency.

Also: pass `cancel_futures=True` to `ThreadPoolExecutor.shutdown()` in the
event-of-clean-exit path. On SIGTERM, the orphaned Python sub-subprocess
continues per PR #201's observed behavior (still desirable — cycle-report
keeps writing), but new futures don't queue once shutdown is signalled.

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

### Step 6 — Fall-back behavior (Vector A C3 + I4 fold)

If `ThreadPoolExecutor` itself raises (rare; e.g., resource exhaustion), the
existing top-level `try` block in `main()` catches and emits the
`SCANNER-CYCLE-SUMMARY` with the partial state.

If a single future raises an unhandled exception inside `future.result()`,
we catch and **increment `state.classification_other_error` and continue the
loop with remaining futures** — one tweet's failure NEVER aborts the batch
(Vector A I4 explicit semantic).

**Vector A C3 fold — 429 cascade handling.** A naive "increment counter +
continue" approach fails when ALL futures hit 429 in burst: the next futures
launch immediately, hit 429 again, the entire cycle's classifier output
collapses to zero alerts dispatched. Mitigation:

Per-worker exponential backoff on HTTP 429:

```python
RETRY_429_DELAYS = [2.0, 4.0, 8.0]  # 3 retries; max 14s of extra wait per tweet

def _classify_one_with_backoff(tweet):
    for attempt, delay in enumerate([0] + RETRY_429_DELAYS):
        if delay > 0:
            time.sleep(delay)
        response = requests.post(...)
        if response.status_code != 429:
            return _process(response)
        with _state_lock:
            state.openrouter_429_burst_count += 1
    # 4 attempts exhausted (initial + 3 retries); count as 4xx
    with _state_lock:
        state.openrouter_4xx += 1
        state.skips += 1
    return None
```

This gives any single tweet up to 14s of backoff before giving up — well
within the cycle budget given concurrency=3 (other workers continue
classifying meanwhile). Also emits `state.openrouter_429_burst_count` for
operator visibility.

Burst-detection ratio in SCANNER-CYCLE-SUMMARY:
- `openrouter-429-burst-count` field added to summary
- Ratio against `tweets-inspected` tells operator if rate-limit is biting

If `openrouter_429_burst_count > 0.5 × tweets_inspected`, operator should
verify the API key tier and consider funding additional credits before
promoting concurrency to 5.

## Safety invariants (MUST hold)

1. **`no_agent: true` cron mode unchanged.**
2. **No secret exposure.** No new `log()` interpolation of `os.environ`, response.text/.headers, HMAC payload, bearer tokens.
3. **No classifier-threshold change.** 0.6 confidence floor preserved.
4. **No `os.popen("curl -H ...")` patterns introduced.** Existing pre-fix `xurl` shell-outs at `:287, :305` (Vector B I1) are pre-existing and out of scope for this PR; filed as follow-up.
5. **`event_id` idempotency semantics unchanged.** No dispatcher-side change.
6. **No DDL / database changes.** Pure Python script edit.
7. **Existing SCANNER-* instrumentation preserved.** Per-stage timings continue to emit.
8. **`hard_extraction_invariant` per-tweet check preserved.**
9. **`narrative_alerts_inbound` write semantics unchanged** (dispatcher unchanged).
10. **No regression for low-volume cycles** — concurrency=3 with <3 tweets just runs them in parallel; identical behavior to sequential for n=1.
11. **(NEW per B-M2 fold) Log surface locked.** Only the existing 6 worker log call sites at `:412/:421/:447/:458/:467/:469` may emit inside the threaded worker. No new threaded-worker log call sites introduced. Each enumerated call site has been audited field-by-field (PR #201 review + this plan's review).
12. **(NEW per B-M1 fold) Traceback module no upgrade.** `traceback.print_exc()` at `:469` either stays as-is OR is replaced with `traceback.format_exception_only(type(e), e)` (locals-free); no upgrade to `cgitb`/`rich`/locals-printing tracebacks.

## Acceptance criteria (pre-registered, post-fold)

For the build + post-deploy verification:

1. **At least one cycle completes in <120s** with a representative classifier workload (20-30 tweets). Evidence: `SCANNER-CYCLE-SUMMARY` line shows `duration-sec < 120.0`.
2. **`jobs.json` `last_status` flips to `"success"`** for the cycle in (1).
3. **All 4 stages emit START + TIMING pairs**.
4. **`narrative-classifier` stage duration** drops from ~222s to under ~80s (concurrency=3 with 25 tweets at ~10s each = ceil(25/3)=9 batches × 10s = ~90s; allowing 1 retry-burst).
5. **`openrouter-4xx` count** stays under 2 per cycle. `openrouter-429-burst-count` stays under `0.2 × tweets-inspected`.
6. **`alerts-dispatched` count and `tweets-inspected` count are within expected ranges** — parallelization must not change behavior, only speed.
7. **No prompt-injection regression.** Journal grep clean.
8. **No secret leakage.** Cycle-report log grep clean.
9. **`fcntl.flock` guard works** — exactly one process per cron-tick window. Simulate by manually launching two scanner invocations in parallel; the second exits immediately with status=0 and emits `SCANNER-CYCLE-SKIP-OVERLAP`.

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
