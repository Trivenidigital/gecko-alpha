**New primitives introduced:** NONE (VPS-only edits to `/home/gecko-agent/.hermes/cron/jobs.json` timeout field and/or `/home/gecko-agent/run-scanner-cycle.py` optimization; new docs-only repo entries in `backlog.md` + `tasks/lessons.md` reflecting the rename and corrected diagnosis).

# Plan: BL-NEW-HERMES-NARRATIVE-CRON-FIX — corrected scope

## Renaming gate

The original backlog name `BL-NEW-HERMES-NARRATIVE-CRON-PROMPT-INJECTION-FIX`
is **stale** based on canonical evidence (per
`feedback_jobs_json_canonical_for_cron_diagnosis.md`). Prompt-injection
blocking was the failure mode on 2026-05-15 only; it was resolved when
the operator refactored the cron from `agent` mode to `no_agent: true`
shell-script mode the same day. **The current failure mode is a
120s cron timeout on the script's actual runtime.**

Renaming the backlog target to:
`BL-NEW-HERMES-NARRATIVE-CRON-TIMEOUT-FIX`

The original name remains as `STATUS=AUDITED-RESOLVED-2026-05-15` so the
audit trail is durable.

## Root cause — current

`/home/gecko-agent/.hermes/cron/jobs.json` for `gecko-x-narrative-scanner`
currently shows:

```
"last_status": "error",
"last_error": "Script timed out after 120s: /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh",
"last_run_at": "2026-05-20T03:02:53.684187+00:00",
"repeat.completed": 147
```

The script is `gecko_x_narrative_scanner.sh` (thin bash wrapper) calling
`/home/gecko-agent/run-scanner-cycle.py`. Last 5 cycle durations:

| Cycle | Duration | Verdict |
|---|---|---|
| 2026-05-20T03:00:53Z | 136.0s | over budget (timeout) |
| 2026-05-20T02:00:53Z | 135.9s | over budget (timeout) |
| 2026-05-20T01:00:52Z | 60.7s | within budget ✓ |
| 2026-05-20T00:00:10Z | 41.2s | within budget ✓ |
| 2026-05-19T23:00:13Z | 56.5s | within budget ✓ |

**Pattern:** intermittent — ~40% of recent cycles exceed 120s budget;
budget-overrun correlates with tweet volume per cycle. The 03:02 cycle
processed 168 tweets across 20 handles, classified 27 new tweets, ~4s per
tweet for OpenRouter classification → ~108s just for classification +
KOL scanning + dispatch = ~136s.

**Quantitative breakdown of the 136s cycle** (from cycle-report log):
- KOL scan (20 handles, sequential `os.popen` to `xurl`): ~10-20s
- Classification (27 tweets, sequential OpenRouter calls, ~4s each): ~108s
- Coin resolver: <1s (1 cashtag-only item; nothing to resolve)
- Alert dispatch (HTTPS POST to gecko-alpha): <1s

The classifier phase dominates. Sequential `os.popen` to OpenRouter is
the bottleneck.

## Safety invariants the fix MUST preserve

1. **No prompt-injection regression.** Cron MUST remain in `no_agent: true`
   shell-script mode. Re-introducing agent mode would re-trigger the May
   15 prompt-injection block.
2. **No secret exposure.** No commands in the cron job, script, or any
   logged output may print `~/.xurl`, bearer tokens, auth headers, raw
   env file contents, API keys, or the HMAC secret.
3. **No classifier-threshold change.** The 0.6 confidence floor is
   business policy and stays.
4. **No KOL-list change.** Out of scope.
5. **No gecko-alpha trading-behavior change.** This work touches only
   the cron scheduler and (optionally) the scanner Python script. Zero
   effect on gate / actionability / capital allocation / classifier
   training.
6. **Hermes safety-scanning stays enabled globally.** The fix MUST NOT
   disable Hermes prompt-injection scanning. (Not applicable because
   we're in `no_agent` mode, but state explicitly.)
7. **No live provider enablement.** No new APIs called.

## Fix options (Option A is the recommended path)

### Option A — Extend the cron timeout (recommended)

Operator's hard constraint allows timeout extension when "causal logs
prove a real post-fix runtime timeout remains." Causal evidence here is
strong:

- 27 tweets × ~4s OpenRouter = 108s structural floor
- Active-KOL cycles can easily push above 150s
- Script work is real (no idle wait); 120s is undersized

Proposed: extend the cron `timeout_s` (or equivalent in jobs.json) from
120s to 240s. This is a single field edit in
`/home/gecko-agent/.hermes/cron/jobs.json`. The script's WORK is unchanged
— the cron handler just gives it room to finish.

**Why 240s and not 180s:** the busiest cycles (e.g., during US daytime
KOL activity, the 03:02 cycle was a UTC night cycle and still hit 136s)
could exceed 180s. 2× current peak gives 2x headroom without being
absurd. Hourly schedule means we still leave 56+ minutes idle per hour.

**Rollback:** edit jobs.json back to 120 (or whatever the original was)
and the next cycle reverts.

### Option B — Parallelize classifier calls (defer)

Replace sequential `os.popen` to OpenRouter with concurrent requests
(e.g., `asyncio.gather` or `concurrent.futures.ThreadPoolExecutor` with
~5 workers). Would compress 108s classification to ~25-30s. Trade-off:
script complexity increase + must rate-limit OpenRouter (free-tier 20
req/min limit). **Defer to a follow-up; not needed for the immediate
fix.**

### Option C — Pre-filter low-signal tweets before classification (defer)

Heuristics: skip tweets without `$` or `0x` or known crypto-narrative
nouns. Would reduce per-cycle classification volume. Trade-off: precision
risk (the operator-tuned 0.6 confidence floor is the canonical filter;
adding a pre-filter introduces a second QC surface that may not align).
**Defer.**

### Option D — Reduce LOOKBACK_MINUTES (defer)

Script currently scans 65 min of tweets per cycle. Could shrink to 60
to match the hourly cron exactly. Cuts ~7% of tweets. Trade-off: cycles
that run a few seconds late lose tweets at the boundary. **Defer.**

## Why Option A is the right immediate fix

1. **Causal logs justify it** (operator's named precondition is met).
2. **Smallest durable change** — single field in one file.
3. **Reversible** — one edit reverts.
4. **No safety surface widened** — same script, same auth, same prompts,
   same network calls.
5. **Aligns with operator's "smallest durable change" rule.**
6. **Frees the operational gap** — once timeout is generous, the cron
   `last_status` should flip to `success` on next cycle, unblocking the
   resolver-health re-check (P5).

Options B/C/D are real optimization opportunities but are **velocity
improvements, not correctness fixes.** File as follow-up backlog entries
post-Option-A merge.

## Verification commands (post-fix)

```bash
# Two-step SSH pattern — write to file, then Read.

# 1. Read updated jobs.json
ssh srilu-vps 'sudo -u gecko-agent cat /home/gecko-agent/.hermes/cron/jobs.json' \
  > /tmp/jobs_after.txt
# Read /tmp/jobs_after.txt — verify timeout field is 240

# 2. Wait for next cron tick (hourly), then verify
ssh srilu-vps 'sudo -u gecko-agent cat /home/gecko-agent/.hermes/cron/jobs.json' \
  > /tmp/jobs_post_tick.txt
# Read /tmp/jobs_post_tick.txt — verify last_status == "success" or
# last_error == null

# 3. Verify the cycle report log shows clean completion
ssh srilu-vps 'sudo -u gecko-agent ls -t /home/gecko-agent/scanner-cycle-report-*.log | head -1' \
  > /tmp/latest_log.txt
# Read /tmp/latest_log.txt to get filename, then:
ssh srilu-vps 'sudo -u gecko-agent tail -30 <FILENAME>' \
  > /tmp/cycle_tail.txt
# Read /tmp/cycle_tail.txt — verify "✅ No blockers" + "Duration: <240.0s"

# 4. Re-check no prompt-injection regression
ssh srilu-vps 'journalctl -u hermes-gateway --since "1 hour ago" --no-pager | grep -iE "prompt.injection|exfil_curl_auth"' \
  > /tmp/injection_recheck.txt
# Read /tmp/injection_recheck.txt — must be EMPTY (no recurrences)

# 5. Verify no secrets in cycle report logs
ssh srilu-vps 'sudo -u gecko-agent grep -iE "bearer|authorization|secret|sk-or-v1|HMAC_SECRET|GECKO_ALPHA" /home/gecko-agent/scanner-cycle-report-*.log | head' \
  > /tmp/secret_recheck.txt
# Read /tmp/secret_recheck.txt — must be EMPTY (script never logs raw secrets)
```

## Post-fix resolver-health check (separate task P5)

After Option A is applied and ≥1 cycle completes with `last_status=success`:

```sql
-- New rows since fix-deploy timestamp
SELECT
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>') AS new_rows,
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>' AND extracted_ca IS NOT NULL) AS new_with_ca,
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>' AND extracted_ca IS NOT NULL AND resolved_coin_id IS NOT NULL) AS new_resolved
FROM narrative_alerts_inbound;
```

If `new_resolved == 0` despite `new_with_ca > 0`, the resolver-writeback
gap is independent of the cron timeout and needs its own root-cause
analysis. File as
`BL-NEW-HERMES-NARRATIVE-RESOLUTION-HEALTH` per the existing operator-
acknowledged backlog item — preserve evidence, do NOT overfit by
combining with this PR.

## OpenRouter / API-key path

The analyst opinion also mentioned an `OPENROUTER...` error in the
classification path (seen in May 15 journal as
`run_agent: Tool execute_code returned error ... ERROR: OPENROUTE...`).
This was during the agent-mode period and has not been observed since
the May 15 refactor. The new `run-scanner-cycle.py` uses direct `os.popen`
shell calls to `xurl` for KOL fetching and (likely) `curl` / similar for
OpenRouter classification.

If the post-fix verification surfaces an OpenRouter error (e.g., expired
API key, model unavailable), document it but **leave a precise operator
action checklist** rather than attempting credential rotation in this PR.

## Rollback

If Option A causes any unexpected behavior:
1. SSH to srilu-vps as gecko-agent
2. Edit `/home/gecko-agent/.hermes/cron/jobs.json` (use the editor that
   doesn't break json; operator's preferred method)
3. Set timeout back to 120s
4. Save; next cron tick uses the reverted value

Total reversion time: <1 minute. No state to clean up; no cascading
effects.

## Deliverables this PR produces

This is a **VPS-only operational fix** plus a **docs-only repo PR**:

1. **VPS edit** (operator applies; not a code commit): `jobs.json` timeout
   field 120 → 240.
2. **Repo PR contents:**
   - `tasks/plan_hermes_narrative_cron_fix_2026_05_20.md` (this file)
   - `tasks/design_hermes_narrative_cron_fix_2026_05_20.md` (created in
     stage P2)
   - `backlog.md` entry update: original
     `BL-NEW-HERMES-NARRATIVE-CRON-PROMPT-INJECTION-FIX` flipped to
     `AUDITED-RESOLVED-2026-05-15`; new entry
     `BL-NEW-HERMES-NARRATIVE-CRON-TIMEOUT-FIX` filed
   - `tasks/lessons.md` update: calibration on jobs.json vs journal grep
   - Memory note already created locally
   - Optional: `BL-NEW-HERMES-NARRATIVE-CRON-OPTIMIZE-CLASSIFIER` (Options
     B+C+D combined as a follow-up)

## Open questions for reviewers

1. **Is 240s the right new ceiling, or should we extend to 300s?** 240s is
   2x current peak with margin. 300s gives more headroom and still leaves
   55 min idle per cron tick.
2. **Should we file the Option B parallelization as a follow-up entry
   THIS PR or defer to operator decision?** Vote for "file now" so the
   shape stays visible.
3. **Is the docs-only repo PR worth it, or is VPS-only sufficient?**
   Memory + backlog updates are valuable durability surfaces; recommend
   yes.
