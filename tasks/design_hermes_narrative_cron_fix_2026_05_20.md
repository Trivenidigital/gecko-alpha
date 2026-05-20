**New primitives introduced:** NONE (this PR ships instrumentation-only via VPS-side edits to `/home/gecko-agent/run-scanner-cycle.py` and `/home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh`; one repo-tracked docs PR with plan/design + backlog flips + lessons + runbook. The Step 4 actual fix — systemd env override OR script parallelization OR combined — lands as a FOLLOWUP PR after Step 2 profiling produces evidence.)

# Design: BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX (Step 1: Instrumentation)

## Design-review fold log (2026-05-20)

Two reviewer vectors completed against design v1. **1 Critical + 4 Important folded.**

| Finding | Vector | Status |
|---|---|---|
| C1: `finally` block does NOT fire under SIGKILL (cron timeout sequence: SIGTERM → grace → SIGKILL is uncatchable). In-flight stage at kill time won't emit, contradicting Gate 2 wording. | A | FOLDED — `_stage()` adds `SCANNER-STAGE-START` emit BEFORE the try block (captures stage entry even when killed mid-stage); Gate 2 wording updated to "≥N completed-stage emits where N = stages completed pre-kill, plus matching number of start emits" |
| I1: Use `time.monotonic()` not `time.time()` for elapsed-duration measurement (NTP-immune, monotonic-correct) | A | FOLDED |
| I2: `Colors.BLUE/RED` ANSI prepend on JSON emits makes report-file JSON harder to grep. (Note: wrapper's sed-strip extracts only the summary line for Telegram; full ANSI body stays in the report file — so this is a report-file cleanliness fix, not a Telegram-body fix.) | A | FOLDED — drop the Colors argument on JSON emits; use plain `print()` instead of `log()` for the SCANNER-STAGE-START/TIMING/CYCLE-SUMMARY lines |
| I3 + F8: Counter `openrouter_other_error` is mis-attributed — broad `except` catches JSON parse, `KeyError`, `verify_hard_extraction_invariant` raises (NOT just OpenRouter). | A + B | FOLDED — rename to `classification_other_error`; OpenRouter-specific counters stay as `openrouter_4xx` and `openrouter_5xx` |
| I4: `_stage()` mutates module-global `state` via closure — acceptable but worth a comment | A | FOLDED — comment added |
| F1: Existing `log(f"... {e}")` callsites unbounded — new code stricter than surrounding | B | NOT FOLDED — out-of-scope per "additive, not boil-ocean"; filed as follow-up `BL-NEW-SCANNER-EXISTING-EXCEPTION-BOUNDING` |
| F10: `print()` callsites in main() FINAL REPORT section inconsistent with `log()` | B | NOT FOLDED — pre-existing, not regressed; filed as follow-up `BL-NEW-SCANNER-PRINT-TO-LOG-CONSISTENCY` |
| M1-M7 (A), M2-M11 (B) | both | Noted; not blocking. M6 (importlib smoke-import) added to pre-deploy syntax check below. |

## Key correctness insight from design review

**SIGKILL is uncatchable.** The cron timeout sequence is SIGTERM (graceful, `finally` fires) → grace period → SIGKILL (uncatchable). If a 136s-cycle hits the 120s budget mid-stage, SIGTERM gives some seconds of grace; if not handled in time, SIGKILL fires. Under SIGKILL, no Python cleanup runs.

The fix: emit a `SCANNER-STAGE-START` line BEFORE the `try:` block in `_stage()`. This captures the moment a stage begins, before any work runs. So even if the stage is killed mid-execution, the report file shows which stage was running at kill time. The completed-stage `SCANNER-STAGE-TIMING` emit (from `finally`) still fires for stages that completed before SIGTERM; SIGKILL stages emit only START (no TIMING).

Operator-readable verification: count of START lines = stages reached; count of TIMING lines = stages completed. Difference = stage in-flight at kill time.

## Scope decision (operator-aligned per plan v3 §"Open questions" #3)

## Scope decision (operator-aligned per plan v3 §"Open questions" #3)

This PR ships **Step 1 (instrumentation) + Step 1.5 (wrapper hardening) ONLY**. Step 4 (the actual fix shape) is DEFERRED to a follow-up PR (`BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY`) after Step 2 profiling produces 3-5 cycles of per-stage timing evidence.

Rationale: instrument-first separates the **observability work** (low-stakes, additive, reversible) from the **fix decision** (which needs evidence to make safely). Lands the observability surface that the operator pinned as a requirement; defers the actual fix until evidence is in.

## Edit 1 — `run-scanner-cycle.py` instrumentation

File: `/home/gecko-agent/run-scanner-cycle.py`
Owner: `gecko-agent:gecko-agent`, mode `0664` (kept; not tightened — Vector A I5 accepted-as-is).

### Hunk 1.1 — Add timing/error fields to `CycleState.__init__`

Find (around lines 18-30):

```python
class CycleState:
    def __init__(self):
        self.handles_scanned = []
        self.tweets_inspected = 0
        self.new_tweets = []
        self.alerts_dispatched = 0
        self.duplicates = 0
        self.skips = 0
        self.speculative_cas_scrubbed = 0
        self.queue_length = 0
        self.total_ops = 0
        self.blockers = []
        self.start_time = datetime.utcnow()
```

Append (inside `__init__` before `state = CycleState()`):

```python
        # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX (2026-05-20):
        # additive observability for per-stage timing diagnosis.
        # JSON-encoded structured emit + kebab-case stage names to avoid
        # MarkdownV1 mangling under deliver=local Telegram path.
        self.stage_timings = {}            # stage-name → elapsed_sec (float)
        self.openrouter_4xx = 0            # 4xx error count from OpenRouter HTTP response
        self.openrouter_5xx = 0            # 5xx error count from OpenRouter HTTP response
        self.classification_other_error = 0  # JSON parse, KeyError, invariant-violation, connection errors (Vector A/B I3/F8 fold)
```

### Hunk 1.2 — Add a `_stage()` wrapper helper

Insert immediately after the `log()` function (around line 45):

```python
def _stage(name, fn, *args, **kwargs):
    """Time a pipeline stage and emit structured log lines.

    Design-review C1 fold: SIGKILL is uncatchable, so a `finally`-only
    pattern misses the in-flight stage at kill time. We emit a START
    line BEFORE the try block so the report-file shows which stage was
    running even if the process is killed mid-stage. The END line emits
    from `finally` for SIGTERM-or-clean exits.

    Design-review I1 fold: time.monotonic() not time.time() (NTP-safe).
    Design-review I2 fold: plain print() (no ANSI Colors) keeps the JSON
    grep-clean in the report file.
    Design-review I4 fold: `state` is module-global; this helper relies
    on that. If ever moved to a module, inject `state` explicitly.

    Stage name MUST be kebab-case to avoid MarkdownV1 italics mangling
    under any future deliver-local Telegram path (Vector A C2 fold).
    """
    import time as _t
    print(json.dumps({
        "event": "SCANNER-STAGE-START",
        "stage": name,
    }))
    t0 = _t.monotonic()
    status = "success"
    error_type = None
    try:
        result = fn(*args, **kwargs)
        return result
    except Exception as e:
        status = "error"
        error_type = type(e).__name__
        raise
    finally:
        elapsed = _t.monotonic() - t0
        state.stage_timings[name] = round(elapsed, 2)
        rec = {
            "event": "SCANNER-STAGE-TIMING",
            "stage": name,
            "elapsed-sec": round(elapsed, 2),
            "status": status,
        }
        if error_type:
            rec["error-type"] = error_type
        # NEVER include args, kwargs, result, or any os.environ value.
        print(json.dumps(rec))
```

### Hunk 1.3 — Wrap pipeline calls with `_stage()`

Find the four `run_kol_watcher` / `classify_tweets` / `resolve_coins` / `dispatch_alerts` invocation sites (in `main()` near the bottom of the file). Wrap each:

```python
# BEFORE
new_tweets = run_kol_watcher(handles, seen_ids)
classified_events = classify_tweets(new_tweets)
resolved_events = resolve_coins(classified_events)
dispatch_alerts(resolved_events)

# AFTER
new_tweets = _stage("kol-watcher", run_kol_watcher, handles, seen_ids)
classified_events = _stage("narrative-classifier", classify_tweets, new_tweets)
resolved_events = _stage("coin-resolver", resolve_coins, classified_events)
_stage("narrative-alert-dispatcher", dispatch_alerts, resolved_events)
```

### Hunk 1.4 — OpenRouter error counter in `classify_tweets`

Find (around line 359):

```python
            if response.status_code != 200:
                log(f"  ✗ API error: {response.status_code}", Colors.RED)
                state.skips += 1
                continue
```

Replace with:

```python
            if response.status_code != 200:
                # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX:
                # separate counters per error class so the post-fix
                # resolver-health re-check can attribute skip causes.
                if 400 <= response.status_code < 500:
                    state.openrouter_4xx += 1
                elif response.status_code >= 500:
                    state.openrouter_5xx += 1
                else:
                    # <400 — likely a redirect or odd status code
                    state.classification_other_error += 1
                # NEVER log response.text or response.headers (Vector B I4).
                print(json.dumps({
                    "event": "SCANNER-OPENROUTER-ERROR",
                    "status-code": response.status_code,
                }))
                state.skips += 1
                continue
```

Also catch connection-level exceptions inside the same `try`:

Find (around line 416):

```python
        except Exception as e:
            log(f"  ✗ Classification error: {e}", Colors.RED)
            import traceback
            traceback.print_exc()
            state.skips += 1
            continue
```

Replace with (preserves existing behavior, adds counter):

```python
        except Exception as e:
            # NEVER include args/kwargs/headers/response-body in this log.
            # Exception message is bounded to type+truncated-str to prevent
            # accidental leakage if a downstream stack-frame held a secret.
            # Vector A/B I3/F8 fold: counter renamed to
            # classification_other_error since this branch catches JSON
            # parse errors, KeyErrors on result['choices'], and
            # verify_hard_extraction_invariant raises — NOT just
            # OpenRouter network/transport failures.
            state.classification_other_error += 1
            print(json.dumps({
                "event": "SCANNER-CLASSIFICATION-ERROR",
                "error-type": type(e).__name__,
                "error-msg-truncated": str(e)[:120],
            }))
            import traceback
            traceback.print_exc()
            state.skips += 1
            continue
```

### Hunk 1.5 — Emit structured cycle summary

Find the FINAL REPORT section (around line 690-720, where `SCANNER_CYCLE:` is emitted). Insert immediately BEFORE the existing `SCANNER_CYCLE:` summary line:

```python
    # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX:
    # JSON-encoded summary for greppability without parsing the colored
    # human-readable section.
    total_duration = (datetime.utcnow() - state.start_time).total_seconds()
    print(json.dumps({
        "event": "SCANNER-CYCLE-SUMMARY",
        "duration-sec": round(total_duration, 2),
        "stage-timings": state.stage_timings,
        "handles-scanned": len(state.handles_scanned),
        "tweets-inspected": state.tweets_inspected,
        "new-tweets": len(state.new_tweets),
        "alerts-dispatched": state.alerts_dispatched,
        "skips": state.skips,
        "duplicates": state.duplicates,
        "speculative-cas-scrubbed": state.speculative_cas_scrubbed,
        "openrouter-4xx": state.openrouter_4xx,
        "openrouter-5xx": state.openrouter_5xx,
        "classification-other-error": state.classification_other_error,
        "blockers": state.blockers,
    }))
```

Leave the existing colored `SCANNER_CYCLE: ...` line in place — it remains the operator-paste-friendly summary; the new JSON line is the machine-grep surface.

## Edit 2 — `gecko_x_narrative_scanner.sh` umask + chmod

File: `/home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh`
Owner: `gecko-agent:gecko-agent`, mode `0750`.

### Current content (verified)

```bash
#!/usr/bin/env bash
set -uo pipefail
cd /home/gecko-agent
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="/home/gecko-agent/scanner-cycle-report-${stamp}.log"
/usr/bin/python3 /home/gecko-agent/run-scanner-cycle.py >"$out" 2>&1
status=$?
summary=$(grep -a 'SCANNER_CYCLE:' "$out" | tail -1 | sed -r 's/\x1B\[[0-9;]*[mK]//g' || true)
if [ -z "$summary" ]; then
  summary="SCANNER_CYCLE: no summary emitted; status=$status"
fi
printf '%s\n' "$summary"
printf 'report=%s\n' "$out"
exit "$status"
```

### Proposed (3 additive lines)

```bash
#!/usr/bin/env bash
set -uo pipefail
umask 0027                                                                  # NEW: defense-in-depth
cd /home/gecko-agent
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="/home/gecko-agent/scanner-cycle-report-${stamp}.log"
: > "$out"                                                                  # NEW: create file FIRST so chmod applies before write
chmod 0640 "$out"                                                           # NEW: explicit 0640 (defense-in-depth alongside umask)
/usr/bin/python3 /home/gecko-agent/run-scanner-cycle.py >"$out" 2>&1
status=$?
summary=$(grep -a 'SCANNER_CYCLE:' "$out" | tail -1 | sed -r 's/\x1B\[[0-9;]*[mK]//g' || true)
if [ -z "$summary" ]; then
  summary="SCANNER_CYCLE: no summary emitted; status=$status"
fi
printf '%s\n' "$summary"
printf 'report=%s\n' "$out"
exit "$status"
```

Behavior change: NONE for the script's exit code, stdout, or work. ONLY the log file's mode flips from `0644` to `0640`.

## Edit 3 — One-shot chmod on existing logs

This is a one-time command run as the operator at deploy:

```bash
ssh srilu-vps 'sudo -u gecko-agent chmod 0640 /home/gecko-agent/scanner-cycle-report-*.log; ls -la /home/gecko-agent/scanner-cycle-report-*.log | head -5' > /tmp/existing_logs_chmod.txt
# Read /tmp/existing_logs_chmod.txt — verify all show mode 0640
```

## Test plan

This is a VPS-only operational fix; no repo unit tests apply. Verification is via the 5 gates from plan v3 §Step 5, run against the deploy.

**Pre-deploy syntax-check (mandatory):**

```bash
# 1. Upload new file to /tmp/run-scanner-cycle.py.new on VPS via scp
# 2. Three-step syntax + import-time check before atomic replace
ssh srilu-vps 'python3 -m py_compile /tmp/run-scanner-cycle.py.new \
  && python3 -c "import ast; ast.parse(open(\"/tmp/run-scanner-cycle.py.new\").read())" \
  && python3 -c "import importlib.util; spec=importlib.util.spec_from_file_location(\"x\",\"/tmp/run-scanner-cycle.py.new\"); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)" 2>&1 \
  && echo SYNTAX_OK' > /tmp/syntax_check.txt
# Read /tmp/syntax_check.txt — MUST show SYNTAX_OK before proceeding.
# The importlib step catches missing deps / top-level NameErrors that
# py_compile + ast.parse miss (Vector A M6 fold).
```

If SYNTAX_OK is not present, halt deploy.

**Post-deploy verification (after first cron tick):**

Per plan v3 §Step 5 — five gates. Halt + rollback on any gate failure.

## Rollback (per-edit)

| Edit | Rollback |
|---|---|
| Edit 1 (run-scanner-cycle.py) | `sudo -u gecko-agent mv /home/gecko-agent/run-scanner-cycle.py.bak.<gitsha>-<unixtime> /home/gecko-agent/run-scanner-cycle.py && chmod 0664` |
| Edit 2 (gecko_x_narrative_scanner.sh) | `sudo -u gecko-agent mv /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh.bak.<unixtime> /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh && chmod 0750` |
| Edit 3 (one-shot log chmod) | `sudo -u gecko-agent chmod 0644 /home/gecko-agent/scanner-cycle-report-*.log` (only reverses; no state to clean) |

No cascading rollback needed — each edit is atomic and reversible independently.

## Safety invariants (recap — from plan v3)

All 11 invariants from plan v3 hold. Highlights for this design's specifics:

- **Invariant 2 (no secret exposure):** Hunks 1.2-1.4 explicitly forbid logging `args`, `kwargs`, `result`, `response.text`, `response.headers`, and `os.environ[*]`. The `_stage()` helper logs only the stage name + elapsed time + status + error TYPE. The OpenRouter error log emits only the HTTP status code (an integer). The classification error log emits the exception type + a `[:120]`-truncated message body — this is bounded and consistent with the existing `Payload: {json.dumps(payload, indent=2)[:200]}...` precedent at line 617 (which the plan accepts as bounded).

- **Invariant 8 (no curl-auth-header patterns):** No new shell-out commands. All HTTP work continues to use `requests.post` / `requests.get` with `Authorization: Bearer` in the `headers` argument — NEVER in argv. The May-15 false-positive class is structurally avoided.

- **Invariant 9 (no log() of secret/sig/canonical/headers/os.environ):** Inspect each new `log()` call — none reference these. The `_stage()` helper takes the function's name + elapsed; it does NOT inspect the function's arguments or return value.

- **Invariant 11 (no markdown special chars):** The new lines are JSON-encoded. Stage names are kebab-case. The `event` values (e.g., `SCANNER-STAGE-TIMING`) are SCREAMING-KEBAB-CASE. There are no asterisks or underscores in keys or values that could trip MarkdownV1.

## Pre-registered acceptance criteria (Step 5 verification gates)

For the SUBSEQUENT cron tick after deploy:

1. **Gate 1 (instrumentation completeness):** for a CLEAN cycle (no timeout): the cycle-report log contains EXACTLY 4 `SCANNER-STAGE-START` lines AND 4 `SCANNER-STAGE-TIMING` lines (matching pairs, one per stage) AND 1 `SCANNER-CYCLE-SUMMARY` line. For a KILLED cycle (cron timeout): `count(START) > count(TIMING)` — the difference is the stage that was in-flight at kill time. SIGKILL skips `finally` (Vector A C1 finding), so the in-flight stage emits START but no TIMING.

2. **Gate 2 (last_status semantics):** `jobs.json` `last_status="success"` is the goal for clean cycles. If a 136s+ cycle still hits the 120s budget (Step 4 fix not yet applied), `last_status="error"` is expected — but the cycle-report log MUST show ≥1 `SCANNER-STAGE-START` line and the count(START)-count(TIMING) delta correctly identifies the killed stage.

3. **Gate 3 (no prompt-injection regression):** zero matches for `prompt.injection|exfil_curl_auth` in journal since fix-deploy-ts.

4. **Gate 4 (no secret leakage):** zero matches for `bearer|authorization|secret|sk-or-v1|HMAC_SECRET|^eyJ` in any cycle-report log since fix-deploy-ts.

5. **Gate 5 (log mode):** every NEW `scanner-cycle-report-*.log` shows mode `0640`, NOT `0644`.

Step 2 (profiling) starts after Gate 5 passes.

## Out-of-scope (deferred to follow-up PRs)

- **Step 4 (actual timeout/runtime fix)** — file as `BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY` once Step 2 evidence is in.
- **No-agent guardrail watchdog** (Vector A I7) — file as `BL-NEW-HERMES-CRON-NO-AGENT-FLAG-WATCHDOG`.
- **Resolver writeback investigation** (P5) — file as `BL-NEW-HERMES-NARRATIVE-RESOLUTION-HEALTH` if Step 6 SQL shows new_resolved=0 despite new_with_ca>0.
- **OpenRouter API-key rotation procedures** — operator credential action; documented in P7 final report if relevant.

## Reviewer focus for the design-review pass (P2.5)

Two vectors against this design (orthogonal to plan-pass vectors):

- **Vector A (Hermes-runtime safety, design-level):** Are the diff hunks correctly placed? Will the `_stage()` decorator's `finally` block fire on `sys.exit()` / SIGTERM? Does the JSON-encoded log line interact correctly with the `Colors.BLUE/RED` ANSI-code prepend (does the markdown delivery still match the JSON cleanly, or does ANSI bleed into the message)?

- **Vector B (prompt + secret safety, design-level):** Does any specific log line in the proposed hunks include a value that could leak under any code path? Verify each `log(json.dumps(...))` call's payload field-by-field. Are the kebab-case event names / stage names truly markdown-safe (verify no asterisks, no underscores, no `_*_`-pair anywhere)?
