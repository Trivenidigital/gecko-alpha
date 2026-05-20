**New primitives introduced:** NONE (VPS-only — additive instrumentation in `/home/gecko-agent/run-scanner-cycle.py`, optional systemd env override in `/etc/systemd/system/hermes-gateway.service` (global to ALL hermes cron jobs), wrapper-script chmod hardening; docs-only repo PR with plan/design + backlog + lessons + memory updates).

# Plan: BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX

## Plan-review fold log (2026-05-20)

Two reviewer vectors completed against plan v2. **4 Critical + 8 Important findings.** All Critical and impl-affecting Important findings folded into this v3 below. Originals at PR #200 reviewer outputs; here are the load-bearing ones:

| Finding | Vector | Status |
|---|---|---|
| C1: `jobs.json` has NO per-job timeout field — the 120s comes from `_get_script_timeout()` in `scheduler.py:664-694`. Real fix path is `HERMES_CRON_SCRIPT_TIMEOUT` env var on the systemd unit OR `cron.script_timeout_seconds` config key. Blast radius is GLOBAL across all hermes cron jobs (currently 1 job, but expansion-prone). | A | FOLDED |
| C2: `deliver: "local"` + `no_agent: true` embeds script stdout into a markdown-formatted message body (`scheduler.py:1117-1124`). Underscored tokens (e.g., `kol_watcher`, `narrative_classifier`) render as MarkdownV1 italics in Telegram delivery. | A | FOLDED — instrumentation uses JSON-encoded structured logs with kebab-case stage names; markdown special chars avoided |
| C1: Cycle-report logs are world-readable (mode 0644). Any new instrumentation = world-readable by default. | B | FOLDED — wrapper script chmod 0640 on log file before Python invocation |
| C2: Existing script line 617 already truncates payload to 200 chars; plan should explicitly assert no instrumentation line references `headers`, `secret`, `sig`, `canonical`, or any `os.environ[*]` value. | B | FOLDED — added to safety invariants below |
| I3: Forbid `os.popen("curl -H 'Authorization: Bearer ...'")` patterns in instrumentation — would re-trigger Hermes scanner if output-time scanning lands. | B | FOLDED — invariant 8 below |
| I4: HMAC secret path is clean; add invariant. | B | FOLDED — invariant 9 below |
| I5: Python syntax-check (`python3 -m py_compile` + `ast.parse`) BEFORE atomic replace. | B | FOLDED — Step 1 deploy procedure |
| I6: Atomic replace via `os.replace` / `mv`, NOT `cp`. | A+B | FOLDED — Step 1 deploy procedure |
| I7: Add `state.openrouter_errors_4xx` + `state.openrouter_errors_5xx` counters to separate skip-by-cause. | B | FOLDED — instrumentation includes these |
| I8: Timeout extension hard cap = `min(2 × p95, 0.5 × cron_interval = 1800s)`. If 2×peak > 1800s, must reduce runtime, not extend. | B | FOLDED — decision rubric updated |
| I4 (A): Decision criterion uses cumulative top-2, not single 60%, to handle dispersed-cost cases. | A | FOLDED — Step 3 rubric updated |
| I5 (A): `run-scanner-cycle.py` mode is 0664 (group-readable). Plan accepts existing mode but tightens log file to 0640. | A | FOLDED |
| I6 (A): Backup file mode mandate 0600 + `chown gecko-agent`. | B | FOLDED |
| I7 (A): No-agent guardrail watchdog. | A | FOLDED into separate follow-up backlog entry; not blocking this PR |
| M8-M11 (A) + M9-M11 (B): notes, deferred. | both | Acknowledged inline |

## Renaming gate (operator-approved 2026-05-20)

Original backlog target `BL-NEW-HERMES-NARRATIVE-CRON-PROMPT-INJECTION-FIX`
is **stale**. Final canonical name: `BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX`.
The original name remains as `STATUS=AUDITED-RESOLVED-2026-05-15`.

## Failure-mode history (point-in-time, bounded — per
`feedback_jobs_json_canonical_for_cron_diagnosis.md` discipline)

| Window | Failure mode | Status |
|---|---|---|
| Pre-2026-05-15 ~14:00 UTC | Hermes prompt-injection scanner false-positive on `exfil_curl_auth_header` (cron in `agent` mode) | **RESOLVED 2026-05-15** by operator refactor to `no_agent: true` shell-script mode |
| 2026-05-15 ~15:00 UTC – present | Real deterministic 120s cron timeout when script's actual runtime exceeds 120s; volume-dependent / intermittent | **CURRENT** — target of this fix |

Evidence the historical mode is fully resolved: zero prompt-injection events in `journalctl -u hermes-gateway --since "5 days ago"` after May 15 14:00.

Evidence the current mode is real: `jobs.json` for `gecko-x-narrative-scanner` shows
```
"last_status": "error",
"last_error": "Script timed out after 120s: /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh",
"last_run_at": "2026-05-20T03:02:53Z"
```

Last 5 cycle durations: 136.0s ⏱, 135.9s ⏱, 60.7s ✓, 41.2s ✓, 56.5s ✓.

## Root cause hypothesis (NOT YET PROVEN — see §"Instrument first")

Inferred (not measured): the 27-tweet × ~4s sequential `requests.post` loop to OpenRouter in `classify_tweets` is the dominant cost. The cycle 03:02 report log shows `Skips (low confidence): 26` with confidence values like 0.05 — meaning 26 classifier calls completed and returned low-confidence results (NOT rate-limited). The dominant cost hypothesis is "27 sequential OpenRouter calls × 4s + KOL scanning + dispatch ≈ 136s." But **this is INFERRED from log tail, not measured** — per-stage timings don't exist yet.

## Operator-pinned fix-shape constraints

1. Profile which script stage consumes time under high tweet volume — instrument BEFORE deciding the fix.
2. Preserve `no_agent: true`.
3. Do NOT touch prompt-injection / security policy except to document as historical.
4. Either reduce runtime under 120s OR raise the cron timeout — extension only allowed if the long path is **legitimate, bounded, and observable**.
5. Add per-stage timing logs so future failures are causal, not ambiguous.
6. Then re-check resolver health.

## Safety invariants (MUST hold across all variants)

1. **No agent-mode regression.** Cron stays `no_agent: true`.
2. **No secret exposure.** Logs MUST NOT contain `~/.xurl/` content, bearer tokens, `Authorization:` headers, raw `.env`, API keys, HMAC secret.
3. **No classifier-threshold change.**
4. **No KOL-list change.**
5. **No gecko-alpha trading-behavior change.**
6. **Hermes safety-scanning stays globally enabled.**
7. **No live provider enablement.**
8. **No `os.popen("curl -H ...")` patterns** in instrumentation (Vector B I3): even though we're in `no_agent` mode and Hermes scanner is prompt-time, output-time scanning is a future possibility; a literal `curl -H "Authorization: Bearer ..."` string in any log line would also be a real secret-exposure surface if argv leaks.
9. **No `log()` call** may interpolate `secret`, `sig`, `canonical`, `headers`, or any `os.environ[*]` value (Vector B I4).
10. **No synthetic paper trades.**
11. **No markdown special chars in instrumentation log lines** (Vector A C2). Stage identifiers use kebab-case (`kol-watcher`, `narrative-classifier`, etc.); the structured-log format is JSON-encoded so underscores INSIDE quoted JSON strings are safe even under MarkdownV1.

## Step 1 — Instrument the script (additive, observable, reversible)

Edit `/home/gecko-agent/run-scanner-cycle.py` additively. Track per-stage durations + OpenRouter error counts. Emit per-stage JSON-encoded structured log lines for greppability.

### Instrumentation shape (proposed)

Add to `CycleState`:

```python
self.stage_timings = {}  # stage-name → elapsed_sec
self.openrouter_4xx = 0  # count
self.openrouter_5xx = 0  # count
```

Wrap each stage with timing:

```python
def _stage(name, fn, *args, **kwargs):
    t0 = time.time()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.time() - t0
        state.stage_timings[name] = elapsed
        log(json.dumps({
            "event": "SCANNER_STAGE_TIMING",
            "stage": name,                     # kebab-case, no underscores
            "elapsed-sec": round(elapsed, 2),
            "status": "success",
        }), Colors.BLUE)
        return result
    except Exception as e:
        elapsed = time.time() - t0
        state.stage_timings[name] = elapsed
        log(json.dumps({
            "event": "SCANNER_STAGE_TIMING",
            "stage": name,
            "elapsed-sec": round(elapsed, 2),
            "status": "error",
            "error-type": type(e).__name__,
        }), Colors.RED)
        raise
```

Invocations:

```python
new_tweets = _stage("kol-watcher", run_kol_watcher, handles, seen_ids)
classified = _stage("narrative-classifier", classify_tweets, new_tweets)
resolved   = _stage("coin-resolver", resolve_coins, classified)
dispatched = _stage("narrative-alert-dispatcher", dispatch_alerts, resolved)
```

OpenRouter error counter inside `classify_tweets`:

```python
if response.status_code != 200:
    if 400 <= response.status_code < 500:
        state.openrouter_4xx += 1
    elif response.status_code >= 500:
        state.openrouter_5xx += 1
    log(json.dumps({
        "event": "SCANNER_OPENROUTER_ERROR",
        "status-code": response.status_code,
        # NEVER log response.text or headers — would leak API metadata
    }), Colors.RED)
    state.skips += 1
    continue
```

Final summary emits a single structured line:

```python
log(json.dumps({
    "event": "SCANNER_CYCLE_SUMMARY",
    "duration-sec": round(total_duration, 2),
    "stage-timings": state.stage_timings,
    "tweets-inspected": state.tweets_inspected,
    "alerts-dispatched": state.alerts_dispatched,
    "openrouter-4xx": state.openrouter_4xx,
    "openrouter-5xx": state.openrouter_5xx,
    "skips": state.skips,
    "duplicates": state.duplicates,
}), Colors.GREEN, bold=True)
```

### Wrapper-script hardening (Step 1.5)

Edit `/home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh` to:
1. `umask 0027` at top so newly-created files default to 0640.
2. Add `chmod 0640 "$out"` immediately after the `>"$out"` redirect creates the file (defense-in-depth).

### Deploy procedure (Step 1)

```bash
# 1. Pull current script as backup with operator-tagged versioning
ssh srilu-vps 'sudo -u gecko-agent cp /home/gecko-agent/run-scanner-cycle.py /home/gecko-agent/run-scanner-cycle.py.bak.<gitsha>-<unixtime> && sudo -u gecko-agent chmod 0600 /home/gecko-agent/run-scanner-cycle.py.bak.<gitsha>-<unixtime>'
# 2. Stage the new version locally (build), upload via scp to /tmp/run-scanner-cycle.py.new on VPS
# 3. Syntax-check on VPS BEFORE replace
ssh srilu-vps 'python3 -m py_compile /tmp/run-scanner-cycle.py.new && python3 -c "import ast; ast.parse(open(\"/tmp/run-scanner-cycle.py.new\").read())" && echo SYNTAX_OK' > /tmp/syntax_check.txt
# Read /tmp/syntax_check.txt — must show SYNTAX_OK before proceeding
# 4. Atomic replace via os.replace semantics (mv, NOT cp)
ssh srilu-vps 'sudo -u gecko-agent mv /tmp/run-scanner-cycle.py.new /home/gecko-agent/run-scanner-cycle.py && sudo -u gecko-agent chmod 0664 /home/gecko-agent/run-scanner-cycle.py'
# 5. Verify
ssh srilu-vps 'sudo -u gecko-agent head -5 /home/gecko-agent/run-scanner-cycle.py; sudo -u gecko-agent stat -c "%a %U:%G %s" /home/gecko-agent/run-scanner-cycle.py' > /tmp/post_replace.txt
```

## Step 2 — Profile 3-5 cycles with instrumentation

Wait for ~3-5 hourly cron ticks. Read the per-stage timings from cycle-report logs. Identify dominant cost.

```bash
# Grab structured timing lines from recent cycles
ssh srilu-vps 'for f in $(sudo -u gecko-agent ls -t /home/gecko-agent/scanner-cycle-report-*.log | head -5); do echo "=== $f ==="; sudo -u gecko-agent grep -a "SCANNER_STAGE_TIMING\|SCANNER_CYCLE_SUMMARY" "$f"; done' > /tmp/profile.txt
# Read /tmp/profile.txt and compute:
# - per-stage mean + p95
# - top-2 cumulative percentage of total
# - openrouter-4xx + openrouter-5xx rates
```

## Step 3 — Decide on evidence (pre-registered rubric)

| Evidence pattern | Verdict | Rationale |
|---|---|---|
| Single stage > 60% of total | (a) or (b) depending on (a-i)/(b-i) below | Single dominant cost — focused fix |
| Top-2 cumulative > 80% of total | (b) | Two stages each significant — parallelize both |
| No single stage > 60% AND top-2 < 80% | (c) | Dispersed cost — combine small reductions + timeout backstop |

(a) Single dominant stage is **structurally unavoidable**: extend timeout via systemd env override. Hard cap: `extension = min(2 × observed_p95, 1800s)`. If 2 × p95 > 1800s, COLLAPSE to (b) — reduction is forced.

(b) Single dominant stage is **reducible** (e.g., classifier parallelization via ThreadPoolExecutor at concurrency=5; OpenRouter's documented rate limit accommodates this): apply reduction, keep 120s timeout.

(c) Dispersed costs: combine small reductions (KOL parallelization, lookback shrink) + accept timeout extension as a safety backstop.

## Step 4 — Apply fix per Step 3 verdict

**Path (a) — systemd env-var timeout extension:**

```
# Operator-applied as root:
# Edit /etc/systemd/system/hermes-gateway.service — add Environment line in [Service] block:
Environment="HERMES_CRON_SCRIPT_TIMEOUT=<computed-from-Step-3>"

# Then:
systemctl daemon-reload
systemctl restart hermes-gateway
```

**Blast-radius note:** This env var affects ALL hermes cron jobs running under `hermes-gateway.service`. Currently only `gecko-x-narrative-scanner` exists (verified). If new jobs are added in the future, they will inherit the higher timeout. This is acceptable for the substrate-class jobs we anticipate; document in repo runbook for future-operator awareness.

**Path (b) — script-level parallelization:**

Replace sequential `for tweet in new_tweets` in `classify_tweets` with `concurrent.futures.ThreadPoolExecutor` at concurrency=5 (under OpenRouter rate-limit headroom). Maintain confidence-floor + hard-extraction-invariant logic per-result. Atomic replace via Step 1 procedure.

**Path (c) — combined:**

Apply path (b)'s parallelization at a milder concurrency=3 + path (a)'s timeout extension to (e.g.) 180s as backstop.

## Step 5 — Verification

Two-step SSH redirect-to-file pattern. Five gates:

```bash
# Gate 1 — instrumentation present
ssh srilu-vps 'sudo -u gecko-agent grep -c "SCANNER_STAGE_TIMING\|SCANNER_CYCLE_SUMMARY" $(sudo -u gecko-agent ls -t /home/gecko-agent/scanner-cycle-report-*.log | head -1)' > /tmp/g1.txt
# Read — count must be >= 5 (4 stage timings + 1 summary)

# Gate 2 — last_status flips success on next cycle
ssh srilu-vps 'sudo -u gecko-agent cat /home/gecko-agent/.hermes/cron/jobs.json' > /tmp/g2.txt
# Read — last_status must be "success", last_error must be null

# Gate 3 — no prompt-injection regression since fix-deploy-ts
ssh srilu-vps 'journalctl -u hermes-gateway --since "<fix-deploy-ts>" --no-pager | grep -iE "prompt.injection|exfil_curl_auth"' > /tmp/g3.txt
# Read — must be EMPTY

# Gate 4 — no secret patterns in cycle-report logs
ssh srilu-vps 'sudo -u gecko-agent grep -iE "bearer|authorization|secret|sk-or-v1|HMAC_SECRET|^eyJ" /home/gecko-agent/scanner-cycle-report-*.log | head' > /tmp/g4.txt
# Read — must be EMPTY

# Gate 5 — log file mode tightened to 0640
ssh srilu-vps 'stat -c "%a %n" /home/gecko-agent/scanner-cycle-report-*.log | head -3' > /tmp/g5.txt
# Read — mode column must show 640 (or 600), NOT 644
```

If any gate fails, halt + roll back (see §Rollback). Do NOT proceed to Step 6.

## Step 6 — Resolver-health re-check (separate task P5)

After ≥3 successful cycles post-fix:

```sql
SELECT
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>') AS new_rows,
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>' AND extracted_ca IS NOT NULL) AS new_with_ca,
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>' AND extracted_ca IS NOT NULL AND resolved_coin_id IS NOT NULL) AS new_resolved,
  COUNT(*) FILTER (WHERE received_at > '<fix-deploy-ts>' AND extracted_cashtag IS NOT NULL AND extracted_ca IS NULL) AS new_cashtag_only
FROM narrative_alerts_inbound;
```

If `new_resolved == 0` despite `new_with_ca > 0`, resolver-writeback gap is INDEPENDENT of cron timeout — file `BL-NEW-HERMES-NARRATIVE-RESOLUTION-HEALTH` with evidence; do NOT combine with this PR.

## Rollback

If Step 1 instrumentation causes any issue:
1. SSH as `gecko-agent`.
2. `mv` the timestamped backup back: `sudo -u gecko-agent mv /home/gecko-agent/run-scanner-cycle.py.bak.<gitsha>-<unixtime> /home/gecko-agent/run-scanner-cycle.py`.
3. Restore mode: `chmod 0664`.
4. Next cron tick uses reverted version.

If Step 4 path (a) (systemd env-var) causes any issue:
1. Edit systemd unit, remove the `HERMES_CRON_SCRIPT_TIMEOUT` Environment line.
2. `systemctl daemon-reload && systemctl restart hermes-gateway`.

If Step 4 path (b) (parallelization) causes any issue:
1. Atomic-replace the script with the pre-Step-4 backup (same procedure as Step 1 rollback).

## Open questions for design-stage reviewers

1. **The new structured-log lines write JSON to stdout, which becomes the Telegram message body via `deliver: "local"`.** Even with kebab-case + JSON-encoded, the message will be ~5 lines of JSON. Will operator find this useful or noise? Defer answering to operator preference — for now, accept the verbose form.

2. **Should we add the no-agent-flag watchdog (Vector A I7) as part of this PR, or file as separate follow-up?** Recommend separate — keeps this PR focused on the timeout fix. File `BL-NEW-HERMES-CRON-NO-AGENT-FLAG-WATCHDOG` post-merge.

3. **Should the wrapper-script chmod 0640 also retroactively chmod the existing world-readable logs?** Recommend yes — a `chmod 0640 /home/gecko-agent/scanner-cycle-report-*.log` one-shot in the deploy procedure. Risk-free; tightens existing exposure.

4. **Should OpenRouter error counters fold into the `state.skips` total, or stay as separate buckets?** Recommend separate. The current `skips` conflates low-confidence + no-JSON + classification-exception + OpenRouter-4xx + OpenRouter-5xx. The Step 6 resolver-health re-check needs these separated to attribute correctly.

## Deliverables this PR produces

1. **VPS edits (operator-applied):**
   - `/home/gecko-agent/run-scanner-cycle.py` — instrumentation additions
   - `/home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh` — umask + chmod hardening
   - `/etc/systemd/system/hermes-gateway.service` — Environment line for `HERMES_CRON_SCRIPT_TIMEOUT` (only if Step 3 verdict (a) or (c))
   - One-shot chmod 0640 on existing `scanner-cycle-report-*.log` files
   - Backup files: `run-scanner-cycle.py.bak.<gitsha>-<unixtime>` (mode 0600, owned gecko-agent)

2. **Repo PR contents:**
   - `tasks/plan_hermes_narrative_cron_fix_2026_05_20.md` (this file)
   - `tasks/design_hermes_narrative_cron_fix_2026_05_20.md` (stage P2 with concrete edits + diff hunks)
   - `backlog.md`:
     - Flip `BL-NEW-HERMES-NARRATIVE-CRON-PROMPT-INJECTION-FIX` → `STATUS=AUDITED-RESOLVED-2026-05-15`
     - File `BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX` as the new active entry
     - File follow-up: `BL-NEW-HERMES-CRON-NO-AGENT-FLAG-WATCHDOG`
   - `tasks/lessons.md`: add the timestamp-bounded query-window calibration lesson
   - Memory files already created locally; index already updated.
