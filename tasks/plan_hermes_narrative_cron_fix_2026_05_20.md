**New primitives introduced:** NONE (VPS-only edits to `/home/gecko-agent/run-scanner-cycle.py` to add per-stage timing instrumentation, and conditionally to `/home/gecko-agent/.hermes/cron/jobs.json` timeout field iff justified by post-instrumentation evidence; docs-only repo PR with plan + design + backlog + lessons updates).

# Plan: BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX

## Renaming gate (operator-approved 2026-05-20)

The original backlog name `BL-NEW-HERMES-NARRATIVE-CRON-PROMPT-INJECTION-FIX`
is **stale** based on canonical evidence (`jobs.json` last_error +
timestamp-bounded journal review). Final canonical name is:

`BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX`

The original name remains as `STATUS=AUDITED-RESOLVED-2026-05-15` for
audit-trail durability.

## Failure-mode history (point-in-time, bounded)

| Window | Failure mode | Status |
|---|---|---|
| Pre-2026-05-15 ~14:00 UTC | Hermes prompt-injection scanner false-positive on `exfil_curl_auth_header` pattern (when cron ran in `agent` mode with an LLM prompt) | **RESOLVED 2026-05-15** by operator refactor to `no_agent: true` shell-script mode |
| 2026-05-15 ~15:00 UTC – present | Real deterministic 120s cron timeout when script's actual runtime exceeds 120s; volume-dependent and intermittent | **CURRENT** — target of this fix |

**Evidence the historical mode is fully resolved:** zero
prompt-injection events in `journalctl -u hermes-gateway --since
"5 days ago"` after May 15 14:00. Compare the timestamp range
before declaring a pattern current.

**Evidence the current mode is real:** `jobs.json` for
`gecko-x-narrative-scanner` shows `last_status="error"`,
`last_error="Script timed out after 120s: ..."` at last_run_at
`2026-05-20T03:02:53Z`. Last 5 cycle durations from
`scanner-cycle-report-*.log` files:

| Cycle (UTC) | Duration | Verdict |
|---|---|---|
| 2026-05-20T03:00:53Z | 136.0s | over budget (timeout) |
| 2026-05-20T02:00:53Z | 135.9s | over budget (timeout) |
| 2026-05-20T01:00:52Z | 60.7s | within budget ✓ |
| 2026-05-20T00:00:10Z | 41.2s | within budget ✓ |
| 2026-05-19T23:00:13Z | 56.5s | within budget ✓ |

~40% of recent cycles exceed 120s. Pattern correlates with tweet volume.

## Root cause hypothesis (not yet PROVEN — see §"Instrument first")

From the 03:02 cycle report log:
- 20 KOL handles scanned
- 168 tweets inspected
- 27 new tweets sent to classifier
- 1 alert dispatched (cashtag-only, $CUM)
- 0 speculative CAs scrubbed

Classifier appears to be the dominant cost: 27 tweets × ~4s OpenRouter
calls (sequential `os.popen`) ≈ 108s. Combined with KOL scanning
(20 handles × `xurl` calls, sequential) + dispatch overhead ≈ 136s.

**But this is INFERRED from log tail, not measured.** The script does NOT
currently emit per-stage timings — only the overall `Duration: 136.0s`.
Inferred attribution may be wrong (e.g., the `os.popen` to `xurl` for
KOL fetching could be the real bottleneck if the X API is slow; the
classifier could be faster than estimated).

## Operator-pinned fix-shape constraints (2026-05-20)

1. **Profile which script stage consumes time under high tweet volume**
   — instrument BEFORE deciding the fix.
2. **Preserve `no_agent: true`.** Re-introducing agent mode would
   re-trigger the May 15 prompt-injection block.
3. **Do NOT touch prompt-injection / security policy** except to document
   it as historical.
4. **Either reduce runtime under 120s OR raise the cron timeout** —
   timeout extension is ONLY justified if the long path is "legitimate,
   bounded, and observable" (i.e., per-stage timings show a real
   structural cost, not a memory leak or regression).
5. **Add per-stage timing logs** so future failures are causal, not
   ambiguous.
6. **Then re-check resolver health.**

## Plan structure (sequence)

### Step 1 — Instrument the script (small, durable, observable)

Add per-stage timing instrumentation to `/home/gecko-agent/run-scanner-cycle.py`:

- `kol_watcher` stage: total time + per-handle breakdown
- `narrative_classifier` stage: total time + per-tweet breakdown (or
  histogram) + count of tweets classified
- `coin_resolver` stage: total time + number of resolutions attempted
  + number succeeded
- `narrative_alert_dispatcher` stage: total time + dispatch count

Emit as a structured `SCANNER_STAGE_TIMING` log line per stage AND in the
final `SCANNER_CYCLE` summary so future failures can be diagnosed from
the cycle log alone without re-running.

**Safety invariants on the instrumentation:**

- No new commands; no new outbound calls; no new shell-outs.
- No logged value contains secrets (API keys, HMAC, bearer tokens, raw
  `~/.xurl` content, raw `.env` content).
- Pure additive — no behavior change to KOL scan, classification,
  resolution, or dispatch.
- Idempotent under re-deploy: replacing the file replaces the script
  atomically; next cron tick uses the new version.

### Step 2 — Profile 3-5 cycles with instrumentation in place

Wait for ~3-5 cron ticks (3-5 hours) to capture varied-volume cycles.
Read the per-stage timings. Identify the dominant cost.

### Step 3 — Decide the fix shape based on evidence

Three outcomes possible:

**(a) Single stage dominates AND is structurally bounded** (e.g.,
classifier is 80% of runtime and the OpenRouter call latency is
inherent): extend the cron timeout to 2× the observed-peak (e.g.,
240s if peak is 136s, or 300s if some cycles hit ~180s). Timeout
extension is now justified per operator's criterion: "long path is
legitimate, bounded, and observable."

**(b) Single stage dominates AND can be reduced** (e.g., classifier is
80% and we can parallelize OpenRouter calls 5x): reduce runtime first,
keep 120s timeout, re-measure. Reduction options:

- Parallelize classifier calls (5-10 concurrent OpenRouter requests via
  `concurrent.futures.ThreadPoolExecutor`)
- Parallelize KOL `xurl` calls
- Pre-filter tweets without crypto-context tokens (`$`, `0x`, narrative
  keywords) BEFORE classification — saves classifier calls
- Shrink LOOKBACK_MINUTES from 65 → 60 to match cron exactly

**(c) Stage costs are dispersed AND no single dominant cost**: combine
small reductions (KOL parallelization + lookback shrink) and accept the
timeout extension as a backstop only.

### Step 4 — Apply fix per §3 verdict

Smallest durable change per the operator's principle.

### Step 5 — Verification

Per the verification commands below (two-step SSH redirect-to-file pattern).

### Step 6 — Resolver-health re-check (separate P5)

After 3-5 successful cycles post-fix, re-query `narrative_alerts_inbound`:

```sql
SELECT
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>') AS new_rows,
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>' AND extracted_ca IS NOT NULL) AS new_with_ca,
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>' AND extracted_ca IS NOT NULL AND resolved_coin_id IS NOT NULL) AS new_resolved
FROM narrative_alerts_inbound;
```

If `new_resolved == 0` despite `new_with_ca > 0`, resolver-writeback gap
is INDEPENDENT of cron timeout — file
`BL-NEW-HERMES-NARRATIVE-RESOLUTION-HEALTH` with the evidence, do NOT
combine with this PR.

## Safety invariants (recap — MUST hold across all fix variants)

1. No prompt-injection regression. Cron MUST stay in `no_agent: true`
   shell-script mode.
2. No secret exposure. Logs MUST NOT contain `~/.xurl`, bearer tokens,
   `Authorization:` headers, raw `.env`, API keys, HMAC secret.
3. No classifier-threshold change.
4. No KOL-list change.
5. No gecko-alpha trading-behavior change.
6. Hermes safety-scanning stays globally enabled.
7. No live provider enablement.
8. No synthetic paper trades.

## Rollback

If Step 1 instrumentation causes any issue:
1. SSH to srilu-vps as `gecko-agent`.
2. Replace `run-scanner-cycle.py` with the pre-fix copy (kept in a
   timestamped backup file alongside, e.g.,
   `run-scanner-cycle.py.bak.<unixtime>`).
3. Next cron tick reverts to the original behavior.

If Step 4 fix (timeout extension or runtime reduction) causes any issue:
- For timeout: edit `jobs.json` back to 120s. Single field; <1 min.
- For runtime reduction: replace `run-scanner-cycle.py` with pre-fix
  backup; next cron tick reverts.

## Verification commands (post-fix)

```bash
# 1. Verify jobs.json (if Option (a) chosen)
ssh srilu-vps 'sudo -u gecko-agent cat /home/gecko-agent/.hermes/cron/jobs.json' \
  > /tmp/jobs_after.txt
# Read /tmp/jobs_after.txt — check timeout field

# 2. After next cron tick (hourly), verify status flip
ssh srilu-vps 'sudo -u gecko-agent cat /home/gecko-agent/.hermes/cron/jobs.json' \
  > /tmp/jobs_post_tick.txt
# Read — last_status should be "success" or last_error null

# 3. Latest cycle report tail (per-stage timings should now appear)
ssh srilu-vps 'sudo -u gecko-agent ls -t /home/gecko-agent/scanner-cycle-report-*.log | head -1' \
  > /tmp/latest_log.txt
# Then: ssh srilu-vps "sudo -u gecko-agent tail -60 <FILENAME>" > /tmp/cycle_tail.txt
# Read — verify SCANNER_STAGE_TIMING lines present + "✅ No blockers" + Duration

# 4. No prompt-injection regression (since fix-deploy-ts)
ssh srilu-vps 'journalctl -u hermes-gateway --since "<fix-deploy-ts>" --no-pager | grep -iE "prompt.injection|exfil_curl_auth"' \
  > /tmp/injection_recheck.txt
# Must be EMPTY

# 5. No secrets in logs (since fix-deploy-ts)
ssh srilu-vps 'sudo -u gecko-agent grep -iE "bearer|authorization|secret|sk-or-v1|HMAC_SECRET" /home/gecko-agent/scanner-cycle-report-*.log | head' \
  > /tmp/secret_recheck.txt
# Must be EMPTY

# 6. jobs.json last 5 cycle durations + last_status (post-instrumentation)
ssh srilu-vps 'sudo -u gecko-agent ls -t /home/gecko-agent/scanner-cycle-report-*.log | head -5 | while read f; do dur=$(sudo -u gecko-agent grep -a "Duration:" "$f" | tail -1 | sed -r "s/\x1B\[[0-9;]*[mK]//g"); echo "$f: $dur"; done' \
  > /tmp/recent_durations.txt
# Read — at least one cycle should show resolved status post-fix
```

## Deliverables this PR produces

1. **VPS edit (Step 1):** `run-scanner-cycle.py` instrumentation. Saved
   with timestamped backup. Operator-applied edit; documented diff in
   the repo PR.
2. **VPS edit (Step 4):** either `jobs.json` timeout field (single
   number) OR `run-scanner-cycle.py` further changes for runtime
   reduction. Documented diff in repo PR.
3. **Repo PR contents:**
   - `tasks/plan_hermes_narrative_cron_fix_2026_05_20.md` (this file)
   - `tasks/design_hermes_narrative_cron_fix_2026_05_20.md` (stage P2)
   - `backlog.md`: flip
     `BL-NEW-HERMES-NARRATIVE-CRON-PROMPT-INJECTION-FIX` →
     `AUDITED-RESOLVED-2026-05-15`. File
     `BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX` as new entry.
   - `tasks/lessons.md`: add calibration lesson about bounding query
     windows when diagnosing cron failure modes.

## Why the instrument-first approach is right

1. **The current 136s number is a single observation per cycle** — we
   don't know which stage owns the cost.
2. **Inferred attribution (classifier ≈ 108s) is unverified** — could be
   over-estimating. A measured profile clarifies.
3. **The operator's criterion for timeout extension** ("long path is
   legitimate, bounded, and observable") requires observability that
   doesn't currently exist.
4. **Per-stage timings are a durable observability surface** that
   prevents future ambiguous failures. Future operators won't need to
   re-run the diagnosis.
5. **Smallest durable change.** Adding logging is reversible, has zero
   behavior change, and structurally improves debuggability.

## Open questions for reviewers

1. Is per-stage timing the right instrumentation, or should we add
   per-tweet timing too (more granular but noisier)? Recommend
   per-stage + summary stats (mean/p95) per stage.
2. Should the new logs be JSON-structured (machine-grep friendly) or
   human-readable (current style)? Recommend JSON.
3. Should the instrumentation PR land FIRST (alone), then a second PR
   for the actual fix? Recommend yes — separates the observability work
   from the fix decision.
4. If Step 3 verdict is (a) "extend timeout," what's the right new
   value? 240s (2x current peak) vs 300s (more headroom)? Defer to
   evidence.

## Pre-registered decision criterion (so reviewers can hold us
accountable)

After Step 2 profiling (3-5 cycles with instrumentation):

- If MAX(per-stage duration) > 60% of total cycle duration AND the
  stage is a single sequential API loop: verdict (b) — reduce runtime.
- If per-stage durations sum to total AND no single stage exceeds 60%:
  verdict (c) — combine small reductions + timeout backstop.
- If MAX(per-stage duration) > 60% AND the stage is structurally
  unavoidable (e.g., 27 sequential OpenRouter calls where parallelism
  is infeasible due to rate limits): verdict (a) — extend timeout to
  2× observed peak.

Document the verdict + evidence inline in the design doc (stage P2).
