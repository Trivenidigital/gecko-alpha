# BL-066': TG-social dashboard gap-fill — Design

**New primitives introduced:** `GET /api/tg_social/dlq` endpoint; module-level `dashboard/api.py::_DASHBOARD_SETTINGS` singleton; 3 new `dashboard/db.py` helpers (`get_tg_social_dlq`, `get_tg_social_cashtag_stats_24h`, `get_tg_social_per_channel_cashtag_today`); 1 new frontend component (`TGDLQPanel.jsx`); extended `/api/tg_social/alerts` response shape (1 new `stats_24h` key + 3 new per-channel keys). NO new DB tables, columns, or settings.

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04 — same check as plan v2):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard endpoint generation / FastAPI scaffolding | None found (closest `webhook-subscriptions` is event delivery, not REST) | Build from scratch (extend existing `dashboard/api.py:create_app`) |
| Telegram-channel monitoring / status visualization | None found | Extend existing `TGAlertsTab.jsx` |
| Dead-letter-queue UI / inspector pattern | None found | New `TGDLQPanel.jsx` + `/api/tg_social/dlq` endpoint |
| Sqlite-to-API adapter / read-only query helpers | None found | Reuse `dashboard/db.py:_ro_db` pattern (mode=ro URI) |
| Signal-flow timeline / message stream display | None found | Reuse existing message table render |
| Real-time message stream | None found | Reuse 15s `setInterval` poll pattern |

**Awesome-hermes-agent ecosystem check:** 4 dashboard repos (`hermes-workspace`, `mission-control`, `hermes-webui`, `hermes-ui`) and 1 monitoring toolkit (`hermes-ai-infrastructure-monitoring-toolkit`) — all general-purpose agent fleet/infra dashboards, not Telegram-channel-status surfaces. None replace deployed `dashboard/`.

**Verdict:** Pure internal dashboard extension. **No Hermes-skill replacement.** Building inline.

---

## Drift grounding (per alignment doc Part 3)

**Read before drafting (verified):**
- `dashboard/api.py:1-100` (factory pattern, `Query` import already present, `_get_scout_db` cache)
- `dashboard/api.py:724-837` (existing `/api/tg_social/alerts` composite handler — extension point)
- `dashboard/db.py:25-58` (`_ro_db` async generator with `file:...?mode=ro` URI; row_factory pattern)
- `dashboard/frontend/components/TGAlertsTab.jsx:62-222` (existing channels table + 24h stat row + recent messages — extension point)
- `dashboard/frontend/App.jsx:185` (tab routing — `tg` already mapped, no new tab)
- `tg_social_dlq` deployed schema (verified via SSH 2026-05-04): `(id, channel_handle, msg_id, raw_text, error_class, error_text, failed_at, retried_at)` with `idx_tg_social_dlq_failed_at` index + 395 historical rows (last entry 2026-04-28; pipeline stable since PR #55)
- `tg_social_signals` deployed schema: `(message_pk, token_id, symbol, mcap_at_sighting, resolution_state, source_channel_handle, alert_sent_at, paper_trade_id, created_at)`
- `paper_trades` deployed schema (verified scout/db.py:557-600): NOT NULL columns are `token_id, symbol, name, chain, signal_type, signal_data, entry_price, amount_usd, quantity, tp_price, sl_price, opened_at`; UNIQUE(token_id, signal_type, opened_at)
- BL-065 dispatcher (`scout/social/telegram/dispatcher.py:430-540`): writes `signal_data` with literal keys `resolution`, `channel_handle`, `cashtag`, `candidate_rank`, `candidates_total`; cap query at `dispatcher.py:282` uses `opened_at >= datetime('now', 'start of day')` (calendar-day semantics — design Task 3 helper MUST mirror)

---

## Test matrix

| ID | Test | Layer | What it pins |
|---|---|---|---|
| T1 | `test_get_tg_social_dlq_returns_recent_failures` | DB helper | Helper returns 1 row; truncates raw_text to 240 chars |
| T2 | `test_get_tg_social_dlq_clamps_limit_to_100` | DB helper | Limit clamping (caller can't ask for 999) |
| T3 | `test_endpoint_tg_social_dlq_returns_json` | Endpoint | HTTP 200 + correct JSON shape |
| T4 | `test_endpoint_tg_social_dlq_empty_returns_empty_list` | Endpoint | Empty state returns `[]`, not 500 |
| T5 | `test_get_tg_social_cashtag_stats_24h_counts_dispatched` | DB helper | Rolling 24h window correct |
| T6 | `test_get_tg_social_per_channel_cashtag_today_returns_counts` | DB helper | Calendar-day grouping by channel |
| T7 | `test_endpoint_tg_social_alerts_includes_cashtag_dispatched_in_stats` | Endpoint | New keys in response |
| T8 | `test_endpoint_tg_social_alerts_existing_keys_preserved` | Endpoint | Backward compat + isinstance type checks (BOOL not int 1) |
| T9 | `test_contract_bl065_signal_data_shape_includes_resolution_and_channel` | Contract | Pins BL-065 signal_data JSON keys; fails loudly on producer drift |

**Build-phase coverage gaps (declared as `@pytest.mark.skip` placeholders so CI surface-counts the gap, matching BL-065 v3 + Bundle A pattern):**

| ID | Test | Why deferred |
|---|---|---|
| T10 | `test_endpoint_tg_social_alerts_when_settings_init_fails` | Requires monkey-patching module-level `_DASHBOARD_SETTINGS=None`. Functional behavior is exercised indirectly by T7 (cap renders default 5); explicit None-path test is paranoia. Implement only if S2 fallback is later seen to fire in production logs. |
| T11 | `test_endpoint_tg_social_alerts_when_cashtag_column_missing` | Requires synthetic pre-BL-065 DB schema; defensive fallback in api.py is the dashboard's escape hatch for the rollback-against-foreign-DB scenario. Implementation low-risk; full test plumbing not justified for the scope. |
| T12 | `test_dlq_panel_renders_truncated_raw_text` | Frontend rendering test; project has no React-test infrastructure (no jest/vitest). Manual smoke-test in browser is acceptance. |

---

## Failure modes (16 — silent-failure-first ordering)

| # | Failure | Silent or loud? | Mitigation in plan v2 | Residual risk |
|---|---|---|---|---|
| F1 | BL-065 dispatcher renames `resolution` → `resolution_kind` | **Silent** (dashboard reports 0) | T9 contract test fails fast | Test must be kept current as BL-065 evolves |
| F2 | `start of day` vs `-24 hours` divergence between dispatcher + dashboard | **Silent** (dashboard lies about cap state) | A1/M2 fix: identical SQL in both | Both must be updated together if ever changed |
| F3 | `Settings()` per-request 500s the alerts endpoint on bad `.env` | **Loud** (HTTP 500) — but on existing surface | A2/M3 fix: module-level singleton + defensive fallback | Singleton reads `.env` once at import; runtime `.env` edits silently ignored (acceptable; restart picks up) |
| F4 | Frontend reads `cashtag_*` keys against unextended API | Loud (undefined render) | M4 fix: `??` defaults + acknowledged coupling | If Task 4 reverts mid-deploy, dashes show; visible but not broken |
| F5 | DB rollback to pre-BL-065 (no `cashtag_trade_eligible` column) | **Loud** (HTTP 500) | S2 fix: defensive try/except → fall back to old shape | Operator sees "all channels: cashtag-eligible no" — correct given missing column |
| F6 | `paper_trades.signal_data` NULL on legacy non-tg_social rows | Silent (json_extract returns NULL → not in WHERE filter) | Filter includes `signal_type = 'tg_social' AND json_extract = 'cashtag'` — NULL automatically excluded | None — SQLite json_extract on NULL returns NULL, NULL = 'cashtag' is false |
| F7 | Malformed JSON in `signal_data` (e.g., truncated mid-write) | Silent (json_extract returns NULL → row excluded) | Acceptable — better to undercount than crash | Rate of malformed JSON unknowable without instrumentation; defer to operator complaint |
| F8 | Read-only DB lock under concurrent writer (Windows file-locking strict) | **Loud** (`database is locked` error in tests) | `_ro_db` uses `file:...?mode=ro` URI — concurrent reader-writer is supported by SQLite WAL (production has WAL on); test inserts use explicit `commit()` | Windows tests may flake; test pattern uses `aiosqlite.connect` block-scoped + commit before reader opens |
| F9 | DLQ `raw_text` contains binary or non-UTF8 | Loud (decode error in JSON serialization) | `(r[3] or "")[:240]` truncates string-or-empty; JSON serializer handles strings | If raw_text is bytes (shouldn't happen per schema TEXT NOT NULL), would fail; acceptable risk |
| F10 | Per-channel count query returns row with NULL channel_handle (json_extract returned NULL) | Silent (row would be in dict with key None) | Helper filters `if r[0]` — None keys excluded | Still possible to get an empty dict if ALL rows have NULL extraction; that's just "no dispatches today" — correct |
| F11 | Dashboard reads `paper_trades` while pipeline writes new row mid-query | None (read-only WAL semantics) | SQLite WAL guarantees readers see consistent snapshot | None |
| F12 | TGDLQPanel 30s poll vs TGAlertsTab 15s poll causes operator confusion | Cosmetic (DLQ shows older counts than messages) | Acceptable per BL-066' scope; documented in panel header (rendered count = visible row count, not server-side count) | If operator confused, unify cadence in follow-up |
| F13 | New endpoint `/api/tg_social/dlq` 404s if route registration failed | **Loud** (HTTP 404) | T3 endpoint test exercises route via httpx; CI catches | None |
| F14 | Frontend bundle is stale (deploy didn't rebuild dist/) | Silent (UI doesn't show new columns) | §5 step 8 visual confirmation is mandatory; build is part of plan Task 5 Step 3 | Operator must run `npm run build` before commit; build artifacts committed per project convention |
| F15 | Cap_per_day defaults to 5 hard-coded if `_DASHBOARD_SETTINGS is None` | Silent (would render `0/5` even if Settings has higher cap) | Acceptable — Settings None means env is broken; fallback to default mirrors current operational state | Operator should never see this in practice (pipeline would have already failed) |
| F16 | `cashtag_trade_eligible` integer 1/0 vs bool True/False mismatch | Silent (downstream typed code might break later) | T8 isinstance check pins bool type | Future refactor that drops `bool()` cast must update T8 |

---

## Performance notes

**Cashtag-today per-channel query** (`json_extract` on `paper_trades.signal_data`):
- SQLite does not auto-index json_extract expressions. Query plan: index seek on `idx_paper_trades_signal (signal_type=?)`, then linear scan over ~today's rows for `signal_type='tg_social'`.
- Scale today: ~0–10 cashtag dispatches/day across 8 channels (per memory `project_bl065_deployed_2026_05_04.md`: "operator-driven enablement still pending; no cashtag dispatches yet").
- Even at 100×: scan over 1000 rows is sub-millisecond.
- No expression index added — premature given cardinality. Revisit if message volume grows 1000× (won't happen organically).
- Same query pattern is used by the dispatcher's gate at `dispatcher.py:282` — they share the same performance characteristic.

**Module-level Settings singleton:**
- `_DASHBOARD_SETTINGS = Settings()` runs once at process import.
- Pydantic Settings reads `.env` from `os.getcwd()` by default; project convention has `.env` in repo root + uvicorn started from `/root/gecko-alpha/`.
- Subsequent endpoint calls reference the cached instance — no syscall cost.
- Trade-off: a runtime `.env` edit is not picked up until `systemctl restart gecko-dashboard`. This matches the rest of the project's operational model.

---

## Rollback

**No DB rollback required.** Zero schema changes; the only persistence touched is the `paper_trades.signal_data` READ contract (already deployed in BL-065).

**Code rollback path:**
```bash
ssh srilu-vps "cd /root/gecko-alpha && systemctl stop gecko-dashboard && git checkout <prev-master-sha> && systemctl start gecko-dashboard"
```

Verification post-rollback:
- `curl -s localhost:8000/api/tg_social/dlq` returns HTTP 404 (endpoint gone — expected)
- `curl -s localhost:8000/api/tg_social/alerts | jq '.stats_24h | keys'` returns the original 6-key set without `cashtag_dispatched`
- TGAlertsTab still renders (frontend doesn't crash on missing keys due to existing graceful handling + `??` defaults from this build)

**Partial rollback (DLQ panel only / cashtag visibility only):** the two clusters (DLQ: Tasks 1+2+6; cashtag: Tasks 3+4+5) are independent at the file level. Operator can `git revert` either commit cluster individually if one ships and the other regresses — see plan v2 Self-Review #8.

---

## Operational verification (§5 — see plan v2)

Plan v2 §5 covers:
- Pre-deploy backup + error baseline capture (S6)
- Stop-FIRST sequence (atomic frontend bundle flip per D3)
- Endpoint reachability + key-presence (not value) checks (D4)
- Post-deploy error delta vs baseline (S6)
- Manual one-shot end-to-end verify (flip → confirm → revert)

Design adds no operational verification beyond plan; this section is here for cross-reference completeness.

---

## Self-Review

1. **Hermes-first present:** ✓ table + ecosystem + verdict per convention.
2. **Drift grounding:** ✓ explicit file:line refs to all extended code; deployed schema verified via SSH.
3. **Test matrix complete:** 9 active + 3 honestly-skipped placeholders covering all surfaces (DB layer, endpoint layer, frontend, contract); aligns with TDD plan tasks.
4. **Failure modes 16/16 silent-failure-first:** F1, F2, F6, F7, F10, F12, F14, F15, F16 are silent (mitigated or accepted with rationale); F3-F5, F8, F9, F11, F13 are loud (visible failure modes).
5. **Performance honest:** json_extract scan acknowledged; no premature indexing; rationale tied to BL-066' cardinality.
6. **Rollback complete:** code rollback path + partial rollback option + verification steps.
7. **No new DB schema:** verified — no migration needed; backward compat preserved by the defensive try/except in api.py for the rollback-foreign-DB scenario.
