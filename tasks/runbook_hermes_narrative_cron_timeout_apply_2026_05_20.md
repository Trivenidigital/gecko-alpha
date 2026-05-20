# Runbook: Hermes narrative scanner cron — classifier parallelization

**Deploy timestamp:** 2026-05-20T04:51:13Z
**Repo branch:** `feat/hermes-narrative-cron-timeout-apply-2026-05-20` @ `f1f3078`
**VPS:** srilu-vps (89.167.116.187)
**Backup:** `/home/gecko-agent/run-scanner-cycle.py.bak.f1f3078-1779252673` (mode 0600, gecko-agent)

## What this PR ships (deployed)

VPS-only edit to `/home/gecko-agent/run-scanner-cycle.py`:

- `concurrent.futures.ThreadPoolExecutor` at `CLASSIFIER_CONCURRENCY=3` for parallel OpenRouter classification
- `threading.Lock`-protected state.* mutations (replaces pre-fix sequential increments)
- Per-worker exponential backoff on HTTP 429 (delays `[2s, 4s, 8s]`)
- `state.openrouter_429_burst_count` counter
- `fcntl.flock(LOCK_EX | LOCK_NB)` overlapping-cycle guard at script entry (emits `SCANNER-CYCLE-SKIP-OVERLAP` + exits 0 on collision)
- `verify_hard_extraction_invariant` refactored to return scrubbed count (worker increments under lock)
- Post-200 work wrapped in single broad-except (closes uncaught exception escape; mirrors prod L489-507 semantics)
- `traceback.print_exc()` dropped from hot exception path (closes locals-via-traceback surface)
- `openrouter-429-burst-count` field added to `SCANNER-CYCLE-SUMMARY`

## Post-deploy verification — first instrumented cycle 2026-05-20T05:00:54Z

| Stage | Pre-fix (04:00 cycle) | Post-fix (05:00 cycle) | Delta |
|---|---|---|---|
| kol-watcher | 12.11s | 11.79s | ≈ |
| **narrative-classifier** | **222.14s** | **67.47s** | **3.3× faster** ✓ |
| coin-resolver | 0.00s | 0.00s | (no CAs both cycles) |
| narrative-alert-dispatcher | 0.02s | 0.01s | ≈ |
| **Total cycle** | **234.28s** | **79.27s** | **3.0× faster, well under 120s budget** ✓ |
| `jobs.json` last_status | error (timeout) | **ok** | ✓ |
| openrouter-4xx | 0 | 0 | ✓ |
| openrouter-5xx | 0 | 0 | ✓ |
| openrouter-429-burst-count | n/a | 0 | ✓ (no rate-limit hit at concurrency=3) |
| classification-other-error | 0 | 1 | acceptable (1/19 tweets — bounded path; broad-except caught + skipped) |
| alerts-dispatched | 2 | 1 | similar rate |
| Gate 3 (prompt-injection regression check) | n/a | empty | ✓ |
| Gate 4 (secret leakage in log) | (env-var NAME only) | (env-var NAME only) | ✓ — only `NARRATIVE_SCANNER_HMAC_SECRET is set` (env-var NAME, not VALUE) matches; pre-existing log line |
| Gate 9 (fcntl.flock collision test) | n/a | inconclusive | low-volume cycles too fast to trigger collision in test setup; structural correctness verified by Vector A review |

## Net result

**`HERMES-NARRATIVE-CRON: APPLY-FIX-SHIPPED / NORMAL-VOLUME-VERIFIED / HIGH-VOLUME-MONITORING-ONGOING`**

The 05:00 cycle proves the fix works on normal-volume cycles (19 new tweets). High-volume cycles (40+ tweets) remain to be observed over the next 24-48h.

## Concurrency tuning — when to promote 3 → 5

Per plan v2 §Step 1, promote only after:
1. ≥5 consecutive cycles complete under 120s at concurrency=3
2. `openrouter-429-burst-count == 0` across all 5 cycles
3. OpenRouter dashboard confirms tier (free or funded; promote to 5 if funded 60 req/min)

## fcntl.flock smoke test result (Gate 9)

Attempted at 2026-05-20T05:03:13Z. Result: **inconclusive** — cycle A completed too fast (no new tweets in its lookback window since the 05:00 cron just consumed them) for cycle B to find the lock held. Both cycles ran cleanly; no overlap actually occurred.

**Structural correctness verified** via Vector A design review:
- `os.open(LOCK_PATH, O_CREAT | O_WRONLY)` returns FD held for lifetime of process
- `fcntl.flock(LOCK_EX | LOCK_NB)` is kernel-enforced exclusive lock
- On `BlockingIOError`, script emits `SCANNER-CYCLE-SKIP-OVERLAP` JSON + exits status=0
- Kernel auto-releases on process exit (clean, SIGTERM, SIGKILL all OK)

A second smoke test can be triggered manually by the operator during a busy KOL window:
```bash
ssh srilu-vps 'nohup sudo -u gecko-agent /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh > /tmp/cycle_A.log 2>&1 &
  sleep 0
  sudo -u gecko-agent /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh > /tmp/cycle_B.log 2>&1'
# Cycle B should emit SCANNER-CYCLE-SKIP-OVERLAP if A is still running.
```

## Rollback

If post-deploy a future cycle shows unexpected behavior:

```bash
ssh srilu-vps '
mv /home/gecko-agent/run-scanner-cycle.py.bak.f1f3078-1779252673 /home/gecko-agent/run-scanner-cycle.py
chmod 0664 /home/gecko-agent/run-scanner-cycle.py
chown gecko-agent:gecko-agent /home/gecko-agent/run-scanner-cycle.py
'
```

Total reversion: <1 minute. Next cron tick uses sequential pre-fix behavior.

## Open operator actions

- **None required.** The fix is live and self-verified.
- **Recommended:** monitor `openrouter-429-burst-count` field in SCANNER-CYCLE-SUMMARY over next 24h. If consistently > 0, do NOT promote concurrency=3→5 until OpenRouter tier is confirmed.
- **Optional follow-up:** the `BL-NEW-HERMES-CRON-NO-AGENT-FLAG-WATCHDOG` script (PR #203, merged at `59e1eee`) can be scheduled via cron for periodic guardrail validation.

## PR-review fold log (2026-05-20)

Two reviewer vectors at PR-stage. **0 Critical + 0 Important + 3 Minor**:

| Finding | Vector | Status |
|---|---|---|
| Deployed file mode 0664 → 0640 | A M1 | **FOLDED on VPS 2026-05-20T05:11Z** (one-line chmod; no PR needed) |
| `datetime.utcnow()` DeprecationWarning leaks into log | A M2 + B M1 | NOT FOLDED — filed as `BL-NEW-SCANNER-DATETIME-UTCNOW-DEPRECATION` follow-up |
| Design doc count drift (9 vs 8 log() calls) | B M2 | NOT FOLDED — pure doc-only; not worth a re-edit |

**Vector A also confirmed empirically:**
- All Critical + Important folds from plan + design stages are PRESENT in deployed code with line refs
- Tweet-19 JSONDecodeError was a real test of the broad-except fold: 1 tweet's malformed response → caught + skipped + counter incremented + structured log + return None. Batch unaffected. Healthy validation.

**Vector B also confirmed:**
- All new JSON emits field-by-field secret-clean
- Lock file present + mode 0640 + zero content
- 1 classification-other-error attributed to JSONDecodeError (bounded message, no secret leak)
