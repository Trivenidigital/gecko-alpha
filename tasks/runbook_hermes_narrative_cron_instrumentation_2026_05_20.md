# Runbook: Hermes narrative scanner cron instrumentation (Step 1)

**Deploy date:** 2026-05-20T03:52Z
**Repo commit:** `3af48d9` (this PR)
**VPS:** srilu-vps (89.167.116.187)
**Affected files (VPS-only):**

- `/home/gecko-agent/run-scanner-cycle.py` (size 32334 → 37529)
- `/home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh` (size 481 → 722)
- One-shot chmod 0640 on existing `/home/gecko-agent/scanner-cycle-report-*.log`

This is a docs-only repo PR. The actual code changes live on the VPS;
this runbook captures the exact remote diffs + verification evidence.

## What this PR ships

Per the plan v3 + design v2 (in this PR), **Step 1 instrumentation only**.
Step 4 (the actual timeout/runtime fix) is filed as separate backlog
entry `BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY`.

## Exact VPS diff — `run-scanner-cycle.py`

| Hunk | Lines | Change |
|---|---|---|
| 1.1 | 25-44 | Add `stage_timings`, `openrouter_4xx`, `openrouter_5xx`, `classification_other_error` to `CycleState.__init__` |
| 1.2 | 49-114 (new function) | Add `_stage(name, fn, *args, **kwargs)` helper with START/END pair JSON emits, `time.monotonic()`, kebab-case stage names, no Colors |
| 1.3 | 762-770 | Wrap 4 pipeline calls with `_stage()` |
| 1.4a | 418-433 | OpenRouter HTTP error branch: separate 4xx/5xx counter increments, emit `SCANNER-OPENROUTER-ERROR` JSON (status-code only, no body/headers) |
| 1.4b | 490-509 | Classification broad-except: counter renamed to `classification_other_error`, emit `SCANNER-CLASSIFICATION-ERROR` JSON with truncated message |
| 1.5 | 794-816 | Emit `SCANNER-CYCLE-SUMMARY` JSON before existing colored summary line |

## Exact VPS diff — `gecko_x_narrative_scanner.sh`

```diff
--- /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh.bak.3af48d9-1779249120
+++ /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh
@@ -1,5 +1,9 @@
 #!/usr/bin/env bash
 set -uo pipefail
+# BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX (2026-05-20):
+# tighten log file mode to 0640 (was 0644 world-readable). umask gives
+# defense-in-depth; explicit chmod is the canonical guarantee.
+umask 0027
 cd /home/gecko-agent
 stamp=$(date -u +%Y%m%dT%H%M%SZ)
 out="/home/gecko-agent/scanner-cycle-report-${stamp}.log"
+: > "$out"
+chmod 0640 "$out"
 /usr/bin/python3 /home/gecko-agent/run-scanner-cycle.py >"$out" 2>&1
```

## Deploy procedure followed (3-step syntax check + atomic mv)

```bash
# 1. Upload to /tmp on VPS via scp
scp run-scanner-cycle.py.new srilu-vps:/tmp/run-scanner-cycle.py.new
scp gecko_x_narrative_scanner.sh.new srilu-vps:/tmp/gecko_x_narrative_scanner.sh.new

# 2. 3-step VPS-side syntax check
ssh srilu-vps 'cp /tmp/run-scanner-cycle.py.new /tmp/run_scanner_cycle_validate.py \
  && python3 -m py_compile /tmp/run_scanner_cycle_validate.py && echo PYCOMPILE_OK \
  && python3 -c "import ast; ast.parse(open(\"/tmp/run_scanner_cycle_validate.py\").read()); print(\"AST_OK\")" \
  && python3 -c "import sys, importlib.util; sys.path.insert(0, \"/tmp\"); \
       spec=importlib.util.spec_from_file_location(\"validate\", \"/tmp/run_scanner_cycle_validate.py\"); \
       m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(\"IMPORTLIB_OK\")"'
# Output observed: PYCOMPILE_OK \n AST_OK \n IMPORTLIB_OK ✓

# 3. Backup + atomic mv (run as root since /tmp/.new owned by root)
ssh srilu-vps "
  cp /home/gecko-agent/run-scanner-cycle.py /home/gecko-agent/run-scanner-cycle.py.bak.3af48d9-1779249120
  chmod 0600 /home/gecko-agent/run-scanner-cycle.py.bak.3af48d9-1779249120
  chown gecko-agent:gecko-agent /home/gecko-agent/run-scanner-cycle.py.bak.3af48d9-1779249120
  cp /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh.bak.3af48d9-1779249120
  chmod 0600 /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh.bak.3af48d9-1779249120
  chown gecko-agent:gecko-agent /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh.bak.3af48d9-1779249120
  chown gecko-agent:gecko-agent /tmp/run-scanner-cycle.py.new /tmp/gecko_x_narrative_scanner.sh.new
  mv /tmp/run-scanner-cycle.py.new /home/gecko-agent/run-scanner-cycle.py
  chmod 0664 /home/gecko-agent/run-scanner-cycle.py
  chown gecko-agent:gecko-agent /home/gecko-agent/run-scanner-cycle.py
  mv /tmp/gecko_x_narrative_scanner.sh.new /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh
  chmod 0750 /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh
  chown gecko-agent:gecko-agent /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh
  chmod 0640 /home/gecko-agent/scanner-cycle-report-*.log
"
```

## Post-deploy verification (5 gates)

First instrumented cron tick: **2026-05-20T04:00:53Z** (cycle completed at 04:02:53Z).

### Gate 1 — instrumentation completeness — PASS

Cycle report `/home/gecko-agent/scanner-cycle-report-20260520T040053Z.log`:

- `SCANNER-STAGE-START` count: 2
- `SCANNER-STAGE-TIMING` count: 1
- `SCANNER-CYCLE-SUMMARY` count: 0

Emit content:

```
{"event": "SCANNER-STAGE-START", "stage": "kol-watcher"}
{"event": "SCANNER-STAGE-TIMING", "stage": "kol-watcher", "elapsed-sec": 12.11, "status": "success"}
{"event": "SCANNER-STAGE-START", "stage": "narrative-classifier"}
```

This is the EXPECTED pattern for a SIGKILL'd cycle per design v2 §"Key correctness insight":
- 2 starts, 1 end → in-flight stage at kill time was `narrative-classifier`
- kol-watcher completed in 12.11s ✓
- narrative-classifier started but did NOT emit TIMING (uncatchable SIGKILL)
- coin-resolver + narrative-alert-dispatcher: never reached

Without the C1 fix (START emit before try block), we'd have had only 1 TIMING and no way to know which stage was killed.

### Gate 2 — `jobs.json` last_status — ERROR (expected)

```
last_status=error
last_error=Script timed out after 120s: /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh
last_run_at=2026-05-20T04:02:53.879823+00:00
completed=148  (was 147 pre-deploy)
```

This is EXPECTED — this PR ships only instrumentation; the actual timeout
fix is `BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY` (follow-up).
Cron `completed` counter incremented (148 vs 147), confirming the
instrumented cycle DID run.

### Gate 3 — no prompt-injection regression — PASS

```bash
journalctl -u hermes-gateway --since "2026-05-20 03:50:00" --no-pager | grep -iE "prompt.injection|exfil_curl_auth"
# Empty output → no regression
```

### Gate 4 — no secret leakage — PASS

Initial broad grep matched `"✓ NARRATIVE_SCANNER_HMAC_SECRET is set"` —
this is the env-var NAME, not the VALUE (printed by the existing
`check_prereqs()` function at line 99). Refined grep for actual secret
patterns (`sk-or-v1`, hex 32+ chars) returns empty. No leak.

### Gate 5 — log file mode 0640 — PASS

```
640 /home/gecko-agent/scanner-cycle-report-20260520T040053Z.log
```

All existing logs also flipped to 0640 by the one-shot chmod.

## Empirical attribution (BONUS — drives Step 4 fix)

The instrumentation immediately validated my INFERRED hypothesis with EMPIRICAL evidence:

| Stage | Observed | Verdict |
|---|---|---|
| kol-watcher | 12.11s success | NOT the bottleneck |
| narrative-classifier | >108s KILLED in-flight | BOTTLENECK (CONFIRMED) |
| coin-resolver | unobserved (downstream of classifier) | gated on classifier |
| narrative-alert-dispatcher | unobserved (downstream) | gated on classifier |

This pins the Step 4 fix shape: **parallelize the OpenRouter classifier loop** per the plan's §Step 3 verdict (b) — single dominant stage IS reducible via concurrent.futures.ThreadPoolExecutor. Step 4 PR will land separately.

## Rollback

| Edit | Procedure |
|---|---|
| `run-scanner-cycle.py` | `ssh srilu-vps 'sudo -u gecko-agent mv /home/gecko-agent/run-scanner-cycle.py.bak.3af48d9-1779249120 /home/gecko-agent/run-scanner-cycle.py && chmod 0664'` |
| `gecko_x_narrative_scanner.sh` | `ssh srilu-vps 'mv /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh.bak.3af48d9-1779249120 /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh && chmod 0750 && chown gecko-agent:gecko-agent /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh'` |
| log mode 0640 (existing files) | `ssh srilu-vps 'sudo -u gecko-agent chmod 0644 /home/gecko-agent/scanner-cycle-report-*.log'` (only reverses; no state to clean) |

Each rollback is atomic, independent, and reversible in <1 minute.

## Open operator-action items

- **Schedule** `BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY` once 3+ cycles of evidence land. Recommended path: classifier parallelization. Fallback path: systemd `HERMES_CRON_SCRIPT_TIMEOUT` env override (capped at 1800s).
- **Resolver-health re-check (Task P5)** is gated on the Step 4 fix landing — until cycles complete cleanly, the classifier doesn't reach the resolver, so we cannot measure resolver writeback health post-fix. File evidence at next milestone.
- **No credential rotation required.** No OpenRouter / HMAC / xurl tokens touched by this PR.
- **No `gecko-pipeline` restart required.** This PR only edits the Hermes-side scanner; gecko-alpha pipeline is unaffected.

## Backups

```
/home/gecko-agent/run-scanner-cycle.py.bak.3af48d9-1779249120 (mode 0600, owned gecko-agent, 32334 bytes)
/home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh.bak.3af48d9-1779249120 (mode 0600, owned gecko-agent, 481 bytes)
```
