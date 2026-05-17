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

### D6. 429 hook is separate from dispatch hook (V14 fold)

`record_dispatch()` measures intent (call rate). `record_429()` measures Telegram's punishment response. The two are separate concerns:

- Intent → bursts can happen without 429 if Telegram is lenient at that exact moment
- Punishment → a single 429 is the only firm pacing trigger per V14 review

Alerter calls `record_dispatch()` BEFORE the HTTP request (instrument the call) AND `record_429()` AFTER the response IF status == 429.

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

Already in plan §Deployment. Key checks:

1. **journalctl retention sanity** before deploy: `ssh srilu-vps 'journalctl --disk-usage; systemctl show systemd-journald | grep -E "MaxRetention|MaxUse"'` → confirm at least 28d.
2. Post-restart: `journalctl -u gecko-pipeline --since "5 minutes ago" -p debug | grep '"event": "tg_dispatch_observed"' | head -3` (note `-p debug`).
3. Install archive cron on srilu.
4. Pre-registered review at 2026-06-14 per `BL-NEW-TG-PACING-DECISION` criteria.
