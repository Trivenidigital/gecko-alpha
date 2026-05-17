**New primitives introduced:** Same as `tasks/plan_tg_burst_profile.md` (cycle 3, commits `697d0e8` + `be6429e`) — `scout/observability/__init__.py` package, `scout/observability/tg_dispatch_counter.py` (`TGDispatchCounter` keyed on `(chat_id, source)` tuple + module-level singleton + `record_dispatch()` + `record_429()` + `reset_for_tests()`), structured log events `tg_dispatch_observed` (debug) / `tg_burst_observed` (warning) / `tg_dispatch_rejected_429` (warning), `TG_BURST_PROFILE_ENABLED: bool = True` Settings field, `source:` kwarg added to `scout.alerter.send_telegram_message`, `scripts/tg_burst_summary.sh` (time-of-day histogram + top-K callsites + 429 correlation, journalctl + archive), `scripts/tg_burst_archive.sh` (weekly cron dumping events to `/var/log/gecko-alpha/tg-burst-archive/`), and a filed follow-up `BL-NEW-TG-PACING-DECISION` with pre-registered criteria.

# Design: BL-NEW-TG-BURST-PROFILE

**Plan reference:** `tasks/plan_tg_burst_profile.md` (commit `be6429e`)
**Pattern source:** measurement-only instrumentation; no behavior change to `send_telegram_message` dispatch flow.
**Plan reviews folded:** V13 (instrumentation correctness) + V14 (signal-vs-noise / decision-bearing data) — see commit `be6429e` body.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram per-recipient send instrumentation / burst metrics | None — Hermes Skill Hub (DevOps + Social Media + Productivity) returns no matching skill 2026-05-17 | Build in-tree. |
| Rolling-window counters | None — stdlib `collections.deque` + `threading.Lock` suffice | Build in-tree. |

awesome-hermes-agent: 404 (consistent). **Verdict:** custom-code path is the right answer for measurement-layer work; no Hermes capability would replace the per-call hook in `scout.alerter`.

---

## Design decisions

### D1. Counter keyed on `(chat_id, source)` tuple (V14 fold)

Per-`chat_id`-only aggregation loses callsite attribution. With 13+ dispatch sites all writing to the same `TELEGRAM_CHAT_ID`, the operator can't tell whether a burst came from BL-064 social fan-out, daily-summary, or auto-suspend. Keying the counter on `(chat_id, source)` keeps the rolling-window semantics per-callsite. `source:` kwarg added to `send_telegram_message` with default `"unattributed"` for legacy callers — hot callsites get explicit labels in a follow-up commit (see plan Task 3 step 3.3).

**V15 S1 fold — caller audit recorded:** all 6 representative callers verified to use 3-positional + keyword form (no positional-collision risk on the new `source` kwarg):

| Site | Pattern |
|---|---|
| `scout/trading/auto_suspend.py:266-273, 322-328` | `send_telegram_message(text, session, settings, parse_mode=None)` |
| `scout/main.py:1526-1537` (daily summary) | `(text, session, settings, parse_mode=None)` |
| `scout/trading/calibrate.py:354-359` | `(text, session, settings, parse_mode=None)` |
| `scout/trading/weekly_digest.py:335-337, 342-347` (chunked) | `(chunk, session, settings, parse_mode=None)` |
| `scout/secondwave/detector.py:283-285` | `(text, session, settings, parse_mode=None)` |
| `scout/trading/tg_alert_dispatch.py:333-339` | `(text, session, settings, parse_mode=None)` |

All six pass `parse_mode` as keyword (post §2.9 fix). Adding `source:` keyword-only is contract-safe.

### D1b. Known false-positive: `weekly_digest` multi-chunk loop (V15 S2 fold)

`scout/trading/weekly_digest.py:334-337` loops `for chunk in chunks: await send_telegram_message(...)`. Each iteration acquires the lock and counts; two consecutive chunks within <1s trip `breached_1s` deterministically every Friday. Same shape applies to any future chunked sender (`scout/main.py:351,434` daily summary).

**Decision:** do NOT suppress at counter-emit time. Document in the decision-criteria table (D5 + plan):

- `breached_1s` events on `source` ∈ {`weekly-digest`, `daily-summary`} are EXPECTED — not a pacing trigger.
- The pre-registered PACE criteria (D5) require either a 429 OR >50/week sustained group-chat bursts, both of which exclude single-source chunk-loop noise.
- Operator analysis via `tg_burst_summary.sh` can `grep -v 'source.*weekly-digest'` if the noise distracts.

This is lighter than implementing suppression and respects the discipline "instrument first, decide later" — we want to SEE the chunked behavior in the data, not hide it.

### D2. Log-level discipline (V13 fold)

`scout/main.py:1373` configures structlog without `filter_by_level` — every level emits to journalctl. Cycle 1 V4#4 fold established the pattern: emit only at INFO+ for routinely-fired events that shouldn't spam.

For cycle 3:
- `tg_dispatch_observed` → **debug** (every TG call; 200-500/day → 5,600-14,000 lines/4 weeks; default-INFO journalctl filters it out)
- `tg_burst_observed` → **warning** (threshold breach; rare, decision-bearing)
- `tg_dispatch_rejected_429` → **warning** (Telegram actually penalized us; firm pacing trigger)

Operator opts into debug visibility via `journalctl -p debug` when investigating.

### D3. `threading.Lock` (not `asyncio.Lock`) (V13 fold)

`scout.alerter.send_telegram_message` is `async def`. The hook `record_dispatch(...)` runs synchronously inside the coroutine BEFORE any `await`. Today gecko-alpha is single-event-loop, so the lock is uncontested. `threading.Lock` works AND survives any future thread-spawn caller (e.g., `scout/trading/*` worker threads). `asyncio.Lock` would break the multi-thread test path and any future thread caller.

Justification comment in code at the import site.

### D4. Group-vs-DM threshold (V13 fold)

Telegram's 20/min limit applies to **group chats** (`chat_id` starts with `-`). 1-on-1 DMs (positive `chat_id`) tolerate ~30/sec per the FAQ. Current production `TELEGRAM_CHAT_ID=6337722878` is a DM (per memory `project_telegram_wired_2026_05_06.md`).

`_is_group_chat(chat_id: str) -> bool` helper checks the leading `-`. The `tg_burst_observed` `breached_1m` flag fires ONLY when `count_1m > 20` AND `is_group is True`. Without this fix, every routine ≥21-msg/min DM batch would emit a false positive.

`breached_1s` (>1/sec) still applies universally — Telegram's per-chat 1msg/sec rule applies to DMs too.

### D5. Pre-registered decision criteria (V14 fold)

PACE-vs-ACCEPT thresholds anchored in the plan's "Decision criteria" section. Filed as `BL-NEW-TG-PACING-DECISION` with `decision-by: 2026-06-14` to ensure the measurement has a clear destination per memory `feedback_pre_registered_hypothesis_anchoring.md` ("measurement-only PRs ship telemetry no one ever queries" failure mode).

### D6. 429 hook is separate from dispatch hook (V14 fold + V15 M2/M3 fold)

`record_dispatch()` measures intent (call rate). `record_429()` measures Telegram's punishment response. The two are separate concerns:

- Intent → bursts can happen without 429 if Telegram is lenient at that exact moment
- Punishment → a single 429 is the only firm pacing trigger per V14 review

**V15 M3 fold — response-stream ordering.** The alerter must `await resp.read()` ONCE (returning bytes), then parse json from those bytes for `retry_after`, then decode-for-logging. Calling both `resp.json()` and `resp.text()` would double-consume the stream.

**V15 M2 fold — instrumentation can't crash the alerter.** Wrap the `record_429()` call in its own `try/except Exception: logger.exception("record_429_failed")` so a structlog regression or import failure in the observability module doesn't poison the existing alerter response handler. The outer alerter-wide try/except would catch it but mis-categorize as a Telegram-side error.

**Final shape inside `send_telegram_message`:**

```python
async with session.post(url, json=payload) as resp:
    body_bytes = await resp.read() if resp.status != 200 else None
    if resp.status == 429 and body_bytes is not None:
        retry_after = None
        try:
            body_json = json.loads(body_bytes)
            retry_after = body_json.get("parameters", {}).get("retry_after")
        except (json.JSONDecodeError, ValueError):
            pass
        if settings.TG_BURST_PROFILE_ENABLED:
            try:
                record_429(
                    str(settings.TELEGRAM_CHAT_ID),
                    source=source,
                    retry_after=retry_after,
                )
            except Exception:
                logger.exception("record_429_failed")
    if resp.status != 200:
        body = (
            body_bytes.decode("utf-8", errors="replace")[:200] if body_bytes else ""
        )
        logger.warning(...)
```

### D7. journalctl + archive script (V14#3 fold)

journalctl default ~30d retention is borderline for a 4-week window AND debug-level events can rotate sooner under load. `tg_burst_archive.sh` weekly cron dumps `tg_dispatch_observed|_burst|_rejected_429` events to `/var/log/gecko-alpha/tg-burst-archive/$(date +%Y-%m-%d).jsonl.gz` with 8-week retention. `tg_burst_summary.sh` reads from BOTH journalctl + archive transparently.

The deployment-verification step in plan §Deployment confirms journalctl retention on srilu BEFORE relying on it.

### D8. In-memory singleton, no DB persistence (acceptable for measurement)

Per memory `feedback_in_memory_telemetry_persistence.md`: module-level counters reset on process restart. For BL-NEW-TG-BURST-PROFILE, this is acceptable because:
1. The structured logs persist across restarts (journalctl + archive)
2. The counter's job is to compute the 1s + 60s rolling counts AT call time; cumulative totals are journalctl-aggregated by the summary script
3. Decision-bearing data lives in the log archive, not the counter

If the operator chooses to PACE after 4 weeks, that PR will introduce persistent counters as needed.

---

## Cross-file invariants

| Invariant | Where enforced | Test |
|---|---|---|
| Every `send_telegram_message` call records a dispatch event when `TG_BURST_PROFILE_ENABLED=True` | `scout/alerter.py` hook | `test_send_telegram_message_records_dispatch_when_enabled` |
| Flag default is True | `scout/config.py` field default | `test_tg_burst_profile_enabled_default_true` |
| DM chat_ids do NOT trigger 1m burst event | `_is_group_chat()` guard in `record_dispatch()` | `test_dm_does_not_trigger_1m_burst` |
| Group chat_ids DO trigger 1m burst above 20 | Same guard, opposite path | `test_group_chat_triggers_1m_burst_above_20` |
| 429 from Telegram emits structured event with retry_after | `record_429()` called from alerter response handler | `test_record_429_emits_rejected_event` |
| Counter is thread-safe under concurrent record | `threading.Lock` held across read+write | `test_thread_safety_under_concurrent_record` (exact-equality, 1000 ops) |

---

## Commit sequence

5 commits, bisect-friendly:

1. `feat(config): TG_BURST_PROFILE_ENABLED setting`
2. `feat(observability): TGDispatchCounter module + record_dispatch/record_429 hooks`
3. `feat(alerter): instrument send_telegram_message with source kwarg + 429 capture`
4. `feat(scripts): tg_burst_summary.sh + tg_burst_archive.sh operator tools`
5. `docs(backlog): close BL-NEW-TG-BURST-PROFILE + file BL-NEW-TG-PACING-DECISION`

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Counter hook adds latency to every TG call | Low | Low | Lock-held window evict + count operations are O(N) where N = 60s of dispatches per (chat,source) — at 500 calls/day spread across 13 callsites and 1 chat, N is small (~10s). Sub-millisecond per call. |
| Debug-level journalctl rotation faster than 4w under burst load | Medium | Low | `tg_burst_archive.sh` weekly cron dumps to disk; 8-week retention; summary script reads from both surfaces transparently. |
| False-positive bursts on the DM chat | Medium | Low | `_is_group_chat()` guard suppresses 1m breach on DMs. 1s breach still applies (Telegram's per-chat limit) — surfaces real per-chat spikes. |
| Operator never runs the summary script → data accumulated but no decision | Medium | Medium | Pre-registered criteria + filed `BL-NEW-TG-PACING-DECISION` with explicit decision-by 2026-06-14. Memory `feedback_pre_registered_hypothesis_anchoring.md` is the prior on this risk class. |
| Counter singleton import-time side effects break test isolation | Low | Low | `reset_for_tests()` helper rebinds the module-level singleton. Tests import only the public surface (no `_counter` import — V13 SHOULD-FIX fold). |
| `source:` kwarg defaults to "unattributed" — hot callsites stay unlabeled until follow-up | Medium | Low | Documented in plan Task 3.3 — hot callsites listed for follow-up commit. Operator can still see aggregate dispatch counts; attribution only matters for burst-source analysis. |

---

## Out of scope

- Active pacing / throttling — explicitly NOT this PR. Decision per `BL-NEW-TG-PACING-DECISION` after 4-week soak.
- DB persistence of counters — measurement is journalctl + archive based.
- Per-callsite labeling of all 13+ existing TG dispatch call sites — `source="unattributed"` default keeps existing callers working; explicit labeling is a follow-up commit (low priority, can be done piecewise as operator finds bursts in the data).
- `tg_dispatch_observed` co-occurrence with auto_suspend / kill_switch events — operator can manually correlate via `journalctl --since` ranges; out-of-scope to script.

---

## Deployment verification (autonomous post-3-reviewer-fold)

V16 fold — the cron install MUST be unconditional (not gated on the operator's read of the retention check). Sequence is:

1. **journalctl retention probe** (informational, not a gate):
   ```
   ssh srilu-vps 'systemctl show systemd-journald | awk -F= "/SystemMaxRetentionUsec|SystemMaxUse/ {print}"'
   ```
   Print result for the deploy log. If retention is unset (default), document the observed disk-usage. The archive cron runs regardless (V16 fold), so a low-retention journald doesn't block the deploy.

2. **Install archive cron unconditionally** (V16 MUST-FIX #1):
   ```
   ssh srilu-vps 'crontab -l 2>/dev/null | grep -v tg_burst_archive | { cat; echo "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"; } | crontab -'
   ssh srilu-vps 'mkdir -p /var/log/gecko-alpha/tg-burst-archive && chmod 0755 /var/log/gecko-alpha/tg-burst-archive'
   ```

3. **Restart + verify hook fires:**
   ```
   ssh srilu-vps 'systemctl restart gecko-pipeline && sleep 5 && systemctl is-active gecko-pipeline'
   ssh srilu-vps 'journalctl -u gecko-pipeline --since "5 minutes ago" -p debug | grep "\"event\": \"tg_dispatch_observed\"" | head -3'
   ```
   Note `-p debug` since `tg_dispatch_observed` is DEBUG-level (V13 fold).

4. **Smoke test the summary script:**
   ```
   ssh srilu-vps '/root/gecko-alpha/scripts/tg_burst_summary.sh 1'
   ```

5. **File memory checkpoint** (V16 MUST-FIX #2) — `project_tg_burst_pacing_checkpoint_2026_06_14.md` in `~/.claude/projects/.../memory/` with the pre-registered criteria + summary-script command + archive-dir pointer.

6. **Pre-registered review at 2026-06-14** per `BL-NEW-TG-PACING-DECISION` criteria.
