# BL-071a': Writer Wiring + DexScreener Fetch — Design Document

**New primitives introduced:** None new beyond what `tasks/plan_bl071a_prime_writer_wiring.md` (v2) declares. This design adds analysis of test coverage, edge cases, performance, rollback, and operational verification — no schema, no new modules, no new log events beyond the plan.

**Companion to:** `tasks/plan_bl071a_prime_writer_wiring.md` (the plan v2 is the implementation contract; this design covers test matrix, failure-mode analysis, performance impact, rollback strategy).

---

## 1. Test coverage matrix

| Behaviour | Test | File |
|---|---|---|
| `fetch_token_fdv` returns first pair's FDV (most-liquid) | `test_fetch_token_fdv_returns_first_pair_fdv` | `tests/test_chain_mcap_fetcher.py` |
| `fetch_token_fdv` returns None on empty pairs list | `test_fetch_token_fdv_returns_none_on_empty_pairs` | same |
| `fetch_token_fdv` returns None on 404 | `test_fetch_token_fdv_returns_none_on_404` | same |
| `fetch_token_fdv` returns None on timeout (no network) | `test_fetch_token_fdv_returns_none_on_timeout` | same — uses stub session |
| `fetch_token_fdv` returns None when fdv field missing | `test_fetch_token_fdv_returns_none_when_pair_lacks_fdv_field` | same |
| `_record_completion` populates `mcap_at_completion` for memecoin | `test_record_completion_populates_mcap_at_completion` | `tests/test_chain_outcomes_hydration.py` |
| `_record_completion` writes NULL when fetcher returns None | `test_record_completion_leaves_mcap_null_when_fetcher_returns_none` | same |
| `_record_completion` SKIPS fetcher for narrative pipeline | `test_record_completion_skips_fetcher_for_narrative_pipeline` | same |
| Hydrator: populated mcap + DS hit → 'hit' + outcome_change_pct | `test_hydrator_resolves_memecoin_via_dexscreener_hit` | same |
| Hydrator: populated mcap + DS miss (below threshold) → 'miss' | `test_hydrator_resolves_memecoin_via_dexscreener_miss` | same |
| Hydrator: DS failure → row stays UNRESOLVED + DEBUG (not WARNING) per-row | `test_hydrator_skips_on_dexscreener_failure` | same |
| Hydrator: coupling-guard (BL-071a' acceptance) | `test_hydrator_coupling_guard` | same |
| Hydrator: superseded Bundle A "silent skip" preserved as `@pytest.mark.skip` | `test_hydrator_silent_skip_when_mcap_at_completion_populated_BUNDLE_A_BEHAVIOUR` | same |
| Hydrator: post-BL-071a' resolves populated mcap via fetcher | `test_hydrator_resolves_populated_mcap_via_fetcher` | same |
| Hydrator: self-creates session if none injected (defense-in-depth, R2-1) | **gap (T1)** | — |
| Hydrator: aging-aware ERROR fires when stuck row > threshold (R1-1) | **gap (T2)** | — |
| Hydrator: cycle-level session-health ERROR fires at >50% failure rate (R1-2) | **gap (T3)** | — |
| Hydrator: NO ERROR when failure rate < 50% OR attempts < 3 (negative case) | **gap (T4)** | — |
| Pre-existing legacy positive path (NULL mcap + populated outcomes row) → 'hit' | `test_hydrator_memecoin_legacy_outcomes_path_hits` (Bundle A) | same |
| Aggregate warning does NOT count narrative rows (Bundle A regression) | `test_hydrator_aggregate_does_not_count_narrative_rows` (Bundle A) | same |

**Test gaps to close in build phase:**
- **T1** — defense-in-depth session self-create: test calls `update_chain_outcomes(db, settings=s, mcap_fetcher=stub)` (NO session kwarg) and verifies the populated-mcap row resolves correctly. The hydrator must create + close its own session internally. Without this test, a future refactor that removes the self-create wrapper goes undetected.
- **T2** — aging-aware ERROR: insert a memecoin row with completed_at older than `2 × CHAIN_CHECK_INTERVAL_SEC`, populated mcap, fetcher returns None. After hydration cycle, monkeypatch-captured logs must include `chain_outcome_ds_persistent_failure` ERROR with `oldest_pending_age_hours` field present.
- **T3** — session-health ERROR: insert ≥3 memecoin rows with populated mcap, fetcher always returns None. After hydration cycle, captured logs must include `chain_tracker_session_unhealthy` ERROR with `failure_rate_pct >= 50`.
- **T4** — negative case for T3: insert 3 rows but fetcher returns valid FDV for 2 of 3 (33% failure rate). After hydration, NO `chain_tracker_session_unhealthy` log should appear (failure rate below 50% threshold).

Build phase MUST add these 4 tests. They cover the v2 reviewer-feedback fixes and prevent regression.

---

## 2. Edge case + failure mode analysis

### Task 1 (mcap_fetcher helper)

**F1.1 — DexScreener returns malformed JSON.** `await resp.json()` would raise `aiohttp.ContentTypeError` or `json.JSONDecodeError`. Caught by the `except (asyncio.TimeoutError, aiohttp.ClientError)` block? No — `json.JSONDecodeError` is a subclass of `ValueError`, not `aiohttp.ClientError`. **Fix needed in build:** widen the except clause to include `(asyncio.TimeoutError, aiohttp.ClientError, ValueError)`. Otherwise a malformed-JSON response would crash the writer.

**F1.2 — DexScreener returns 200 but empty body.** `resp.json()` raises `aiohttp.ContentTypeError` (no content-type header) or returns `None`. Plan handles via `data.get("pairs")` returning None → returns None. Safe.

**F1.3 — DexScreener returns 200 with `{"pairs": null}`.** `data.get("pairs")` returns None, `pairs` falsy → returns None. Safe.

**F1.4 — Pair object is a string (not dict).** `pairs[0].get("fdv")` raises AttributeError. Plan handles: `if isinstance(pairs[0], dict)`. Safe.

**F1.5 — FDV is a string ("1500000.0" instead of number).** `float(fdv_raw)` succeeds. Safe.

**F1.6 — FDV is a dict (e.g., `{"usd": 1500000}`).** `float({})` raises TypeError. Plan handles via `except (TypeError, ValueError)`. Safe.

### Task 2 (writer wiring)

**F2.1 — Multiple memecoin completions in one `check_chains` call.** Each calls `_record_completion`, each holds the SQLite write lock for up to 15s during DS fetch. Worst case 4 completions × 15s = 60s of cumulative lock-hold (entire pipeline cycle). Documented as deferred-to-BL-071a'' optimization. **In practice:** 0-2 completions per cycle in current prod (verified via journalctl).

**F2.2 — Fetcher raises a non-aiohttp exception (e.g., asyncio.CancelledError).** The `except Exception` block catches it. CancelledError is BaseException, NOT Exception. Bug: pipeline cancellation during DS fetch would propagate out. **Fix needed in build:** `except (Exception, asyncio.CancelledError)` — wait, no. CancelledError SHOULD propagate so the loop can shut down cleanly. The current code is correct; document this in the comment.

**F2.3 — Narrative pipeline accidentally gets a contract-address token_id.** Writer's `if chain.pipeline == "memecoin"` check is correct — narrative rows skip the fetch. Misrouted pipeline detection is deferred (R1-4). Today this fails silently (NULL mcap → legacy fallback path → also silent). Acceptable; future work captured.

**F2.4 — `chain.completed_at is None` when calling `_record_completion`.** The fallback `(chain.completed_at or datetime.now(timezone.utc))` handles this. Pre-existing behaviour from Bundle A; no regression.

**F2.5 — DexScreener rate limit (429).** `fetch_token_fdv` treats 429 as `status != 200` → returns None. Same handling as 404. Safe but undifferentiated. **Build phase consideration:** add an `if resp.status == 429: logger.debug("ds_rate_limited", ...)` so persistent rate limiting shows up in DEBUG even though we still fail-soft.

### Task 3 (hydrator)

**F3.1 — Settings is None and CHAIN_CHECK_INTERVAL_SEC accessed.** `getattr` with default. Plan uses `if settings is not None else 300`. Safe.

**F3.2 — Hydrator runs while a separate writer cycle is mid-fetch.** Both call `_record_completion` and `update_chain_outcomes` use the same DB connection (single-process). aiosqlite serializes. The hydrator's `BEGIN` (implicit on first write) waits for the writer's transaction to commit. Worst case: the LEARN cycle waits 60s for a busy check_chains cycle. Acceptable.

**F3.3 — Hydrator's self-created session doesn't get closed on exception.** Plan uses `try: ... finally: if own_session: await session.close()`. Safe.

**F3.4 — Persistent-failure age calculation: `completed_at` is naive datetime.** Plan handles via `if completed_at.tzinfo is None: completed_at = completed_at.replace(tzinfo=timezone.utc)`. Safe.

**F3.5 — Hit threshold edge: `outcome_change_pct == 50.0` exactly.** Plan uses `>=` — so exactly +50% is a hit. Documented behaviour; matches the "≥ threshold" semantic.

**F3.6 — `mcap_at_completion = 0.0001` (tiny but populated).** Hydrator's `if mcap_at_completion > 0` accepts it. Tiny mcap × +50% threshold could trip on noise. **Build phase consideration:** add a min-mcap floor (e.g., `>= 1000.0`) to prevent dust-mcap rows from generating bogus hit/miss signals. Could be `CHAIN_OUTCOME_MIN_MCAP_USD: float = 1000.0` setting.

**F3.7 — Persistent-failure ERROR fires every cycle for the same stuck row.** Once a row passes the age threshold, it remains stuck across ALL future cycles until either DS recovers OR the row is manually purged. The ERROR will fire every LEARN cycle — that's the intent (operators see the alarm), but it could become noise after 3+ cycles. **Build phase consideration:** rate-limit by NOT logging if `oldest_pending_age_hours == previous_cycle_oldest_pending_age_hours + cycle_interval` (i.e., same rows still stuck, no escalation). Defer to BL-071a''.

### Task 4 (caller wiring)

**F4.1 — `learner.py:326` runs in a context where the function it lives in has crashed before it (in the same try block).** Plan's change is one-line; the surrounding try/except already handles failures. Safe.

**F4.2 — `settings` shadowed by local variable named `settings`.** Verified: the function imports `from scout.config import get_settings` and assigns `settings = get_settings()` at line 319. The new call at 326 references the same `settings` variable. Safe.

---

## 3. Performance considerations

| Change | Per-cycle cost | Per-LEARN cost | Notes |
|---|---|---|---|
| `fetch_token_fdv` helper | n/a | one HTTP call per memecoin chain_match resolution | 15s timeout; typical 200ms |
| `_record_completion` DS fetch (write time) | One HTTP call per memecoin completion | n/a | Holds SQLite write lock during fetch (deferred to BL-071a'' to fix) |
| `update_chain_outcomes` DS fetch (hydration) | n/a | One HTTP call per pending memecoin row | Self-creates session if none injected |
| Aging tracking + session-health counters | Negligible (a few int additions per row) | Negligible | Pure in-memory |
| Aggregate WARNING/ERROR emits | One log line each per cycle | Same | `if memecoin_X:` guards prevent zero-count logs |

**Worst-case LEARN cycle:** 50 pending memecoin rows, all populated mcap. 50 sequential HTTP calls to DS = ~10s assuming 200ms each, ~750s if all timeout. **In practice:** DS responds fast and most rows get resolved on first try. Pending count after each cycle drops as rows get resolved.

**Should we parallelize hydration fetches?** With 50 rows and ~200ms each, sequential = 10s. Parallel via `asyncio.gather` = 200ms but stresses DS rate limit. Plan keeps sequential for safety. If LEARN cycle starts taking >60s, batch-parallelize in groups of 5.

---

## 4. Rollback strategy

| Item | Rollback method | Side effects |
|---|---|---|
| Task 1 — helper module | `git revert <commit>`, restart service | None — module simply unused. |
| Task 2 — writer wiring | `git revert <commit>`, restart service | New chain_matches will write NULL mcap_at_completion (Bundle A behaviour). Existing rows untouched. Hydrator's populated-row branch becomes unreachable for newly-completed chains; falls back to legacy outcomes. |
| Task 3 — hydrator | `git revert <commit>`, restart service | Hydrator reverts to Bundle A's "silent skip" on populated rows. Aggregate warnings stop firing. Existing data untouched. |
| Task 4 — learner caller | `git revert <commit>`, restart service | Hydrator runs without settings → uses default 50.0 threshold. No threshold-config flexibility but functionally identical. |

**Combined rollback:** Revert all 4 commits (any order). Hydrator drops back to Bundle A behaviour. Existing data, schema, paper_migrations untouched. **No one-way doors** — entire BL-071a' is purely-additive code on top of Bundle A's column.

---

## 5. Operational verification post-deploy

After `git pull` + `systemctl restart gecko-pipeline`:

1. **Pre-deploy backup:** `cp /root/gecko-alpha/scout.db /root/gecko-alpha/scout.db.bak.$(date +%s)` (standard hygiene).
2. **Service started cleanly:** `systemctl status gecko-pipeline` — active+running.
3. **Existing tests' deployment path verification:** `journalctl -u gecko-pipeline --since '5 min ago' | grep -iE "error|exception|traceback"` returns 0 or only pre-existing known-noise entries.
4. **First completion event:** wait for next memecoin chain to complete (rare event; may take hours). When it does, verify in journalctl: `chain_complete event_data.mcap_at_completion=<float>` instead of NULL.
5. **First LEARN cycle (~24h):** look for `chain_outcomes_hydrated count=N` for memecoin pipeline; absence of `chain_outcomes_unhydrateable_memecoin` for newly-completed (post-deploy) rows.
6. **Manual SQL backfill (one-shot, post-verification):** run the SQL at the bottom of the plan to clear the 30 pre-Bundle-A stuck rows.
7. **Health check:** `chain_tracker_session_unhealthy` ERROR should NOT fire under normal conditions. If it does, investigate DS API key / rate limit / network.

---

## 6. Open questions / non-blocking items

1. **Hit threshold value (50%).** Default in plan. Operator-tunable post-deploy via env. If 30-day backtest data shows 50% is too aggressive (most "hits" are 75%+) or too conservative (most pumps die at 30%), operator adjusts via `.env`. Out of plan scope.
2. **DexScreener rate limit unknowns.** Free tier limit not documented publicly. If we hit 429s often, the aging-aware ERROR will fire and operators investigate. Sufficient for now.
3. **Backfill cadence for the manual SQL one-shot.** Run once after verification, then again only if the warning re-appears for old rows.

---

## 7. Deliberate counter-decisions on plan-review feedback

(See plan v2 top "Explicitly deferred to BL-071a''" — three items with rationale.)

## 8. Self-review

- [x] All test gaps explicitly named with build-phase actions (T1–T4)
- [x] All failure modes analyzed for each task (Task 1: 6, Task 2: 5, Task 3: 7, Task 4: 2)
- [x] Performance impact quantified (§3)
- [x] Rollback strategy for each task + combined (§4)
- [x] Operational verification checklist with pre-deploy backup + manual SQL backfill (§5)
- [x] Open questions explicitly flagged as non-blocking (§6)
- [x] No new primitives beyond plan
- [x] Build-phase additions noted: T1-T4 tests, F1.1 widen except clause, F2.5 429 debug log, F3.6 mcap floor consideration
