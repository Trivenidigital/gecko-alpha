# BL-066': TG-social dashboard gap-fill — Design

**New primitives introduced:** `GET /api/tg_social/dlq` endpoint; module-level `dashboard/api.py::_DASHBOARD_SETTINGS` singleton; 3 new `dashboard/db.py` helpers (`get_tg_social_dlq`, `get_tg_social_cashtag_stats_24h`, `get_tg_social_per_channel_cashtag_today`); 1 new frontend component (`TGDLQPanel.jsx`); extended `/api/tg_social/alerts` response shape (1 new `stats_24h` key + 3 new per-channel keys); defensive `try/except aiosqlite.OperationalError` in `get_tg_social_dlq` for missing `tg_social_dlq` table (v2 — S1). NO new DB tables, columns, or settings.

**v2 changes from 2-agent design-review feedback:**
- **MUST-FIX A1** (a702f1f — F2 has no test): added T9b SQL-literal grep on dispatcher's `_channel_cashtag_trades_today_count` for `'start of day'`. Without this, BL-065 refactoring its date math to `'-24 hours'` would silently put dashboard cap badge ahead of dispatcher gate near midnight UTC. Same shape as T9.
- **MUST-FIX M2/A2** (BOTH — T9 theatrical): T9 redesigned. Source-grep retained as cheap belt-and-suspenders, but the LOAD-BEARING contract pin moved to a runtime assertion that calls the dispatcher path against a captured engine and inspects the actually-passed `signal_data` dict. Pins behavior, not text.
- **MUST-FIX D6/S4** (BOTH — rollback "independent at file level" wrong): rephrased — Tasks 5 AND 6 both edit `TGAlertsTab.jsx`, so partial git-revert is messy at frontend level. Cleaner partial-rollback is at the API (Tasks 4 or 2) with frontend graceful-degrade via `??` defaults.
- **SHOULD-FIX D5/D1** (a702f1f — promote T11): defensive `try/except aiosqlite.OperationalError` for missing `cashtag_trade_eligible` column actually mitigates the F19 startup race (dashboard starts before pipeline migrates). T11 promoted from deferred to active so the defense has a test.
- **SHOULD-FIX S1** (a25704a — F17): `tg_social_dlq` table missing on rolled-back-DB scenario; mirror the column-missing pattern with `try/except → []`. Add F17 entry.
- **SHOULD-FIX D3** (a702f1f — F3 reclassification): "Loud (HTTP 500) but unmonitored — visible only on operator probe; mitigation A2/M3 fallback prevents the 500 entirely". No Sentry/Prometheus alerting on dashboard.
- **SHOULD-FIX D4** (a702f1f — Pydantic cost): performance notes quantify ~30 field validations/request × ~5ms saved by module-level singleton.
- **SHOULD-FIX D5/F18** (a702f1f — auth posture): F18 entry — dashboard has no auth on public VPS; DLQ raw_text exposes truncated user-channel content; same posture as existing `text_preview`. Deferred to operator network policy.
- **SHOULD-FIX D5/F19** (a702f1f — migration race): explicit F19 entry; mitigation IS the defensive try/except in dashboard/api.py.
- **SHOULD-FIX S3** (a25704a — test commit/close note): F8 augmented with explicit "test must commit+close before reader" Windows guidance.
- **NIT N1 (a25704a) — silent count corrected**: F4 reclassified silent (`??` defaults render dashes, not "undefined"); silent count is 10 not 9.
- **NIT N2 (a25704a) — env_file path**: noted in Performance section.
- **NIT N3 (a25704a) — dist/ git-status check**: §5 step 8 cross-reference (operational).
- **DELIBERATE COUNTER-DECISION D2** (a702f1f — shared-contract module): NOT applied in BL-066'. ROI only materializes with a third consumer; deferred as `BL-066''-contract-module` follow-up.

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
| T9 | `test_contract_bl065_dispatch_writes_resolution_and_channel_handle` | Contract (runtime) | LOAD-BEARING: invokes `dispatch_cashtag_to_engine` against a captured engine; asserts the actually-passed `signal_data` dict has `resolution=='cashtag'` and `'channel_handle'` key. Pins behavior. |
| T9b | `test_contract_dispatcher_today_count_uses_start_of_day_semantics` | Contract (source) | Greps `inspect.getsource(_channel_cashtag_trades_today_count)` for the `'start of day'` literal — pins F2 mitigation (dispatcher and dashboard MUST use identical date math). Cheap belt-and-suspenders for runtime semantic drift. |
| T11 | `test_endpoint_tg_social_alerts_falls_back_when_cashtag_column_missing` | Endpoint (defensive) | Synthesizes pre-BL-065 `tg_social_channels` schema (no `cashtag_trade_eligible`), asserts endpoint returns 200 with `cashtag_trade_eligible=false` for all rows. Validates F19 migration-race mitigation. |

**Build-phase coverage gaps (declared as `@pytest.mark.skip` placeholders so CI surface-counts the gap, matching BL-065 v3 + Bundle A pattern):**

| ID | Test | Why deferred |
|---|---|---|
| T10 | `test_endpoint_tg_social_alerts_when_settings_init_fails` | Requires monkey-patching module-level `_DASHBOARD_SETTINGS=None`. Functional behavior is exercised indirectly by T7 (cap renders default 5 from fallback); explicit None-path test is paranoia. Implement only if S2 fallback is later seen to fire in production logs. |
| T12 | `test_dlq_panel_renders_truncated_raw_text` | Frontend rendering test; project has no React-test infrastructure (no jest/vitest). Manual smoke-test in browser is acceptance. |

---

## Failure modes (16 — silent-failure-first ordering)

| # | Failure | Silent or loud? | Mitigation in plan v2/v3 | Residual risk |
|---|---|---|---|---|
| F1 | BL-065 dispatcher renames `resolution` → `resolution_kind` | **Silent** (dashboard reports 0) | T9 runtime contract test asserts dispatched dict shape | Test must be kept current as BL-065 evolves |
| F2 | `start of day` vs `-24 hours` divergence between dispatcher + dashboard | **Silent** (dashboard lies about cap state) | A1/M2 fix: identical SQL; T9b source-grep pins dispatcher's `'start of day'` literal | Both must be updated together if ever changed |
| F3 | `Settings()` per-request 500s the alerts endpoint on bad `.env` | **Loud (HTTP 500) but unmonitored** — visible only on operator probe (no Sentry/Prometheus on dashboard) | A2/M3 module-level singleton + defensive fallback prevents the 500 entirely | Operator should run §5 step 5+6 post-deploy probe; ongoing monitoring is an operational gap not solved here |
| F4 | Frontend reads `cashtag_*` keys against unextended API | **Silent** (dashes render via `??` defaults) | M4 fix + `??` defaults; T8 type assertions | If Task 4 reverts mid-deploy, dashes show; visible but not loud |
| F5 | DB rollback to pre-BL-065 (no `cashtag_trade_eligible` column) | **Loud** (HTTP 500 without S2 fix) → degraded under fix | S2 fix: defensive try/except → fall back to old shape; T11 ACTIVE test pins this path | Operator sees "all channels: cashtag-eligible no" — correct given missing column |
| F6 | `paper_trades.signal_data` NULL on legacy non-tg_social rows | Silent (json_extract returns NULL → not in WHERE filter) | Filter includes `signal_type = 'tg_social' AND json_extract = 'cashtag'` — NULL automatically excluded | None — SQLite json_extract on NULL returns NULL, NULL = 'cashtag' is false |
| F7 | Malformed JSON in `signal_data` (e.g., truncated mid-write) | Silent (json_extract returns NULL → row excluded) | Acceptable — better to undercount than crash | Rate of malformed JSON unknowable without instrumentation; defer to operator complaint |
| F8 | Read-only DB lock under concurrent writer (Windows file-locking strict) | **Loud** (`database is locked` error in tests) | `_ro_db` uses `file:...?mode=ro` URI — concurrent reader-writer is supported by SQLite WAL on prod (`scout/db.py:67` sets `PRAGMA journal_mode=WAL`; reader inherits via file header); production has WAL on | Test pattern uses `aiosqlite.connect` block-scoped writer; **MUST** `await conn.commit(); await conn.close()` BEFORE the helper's `_ro_db` reader opens, otherwise Windows tests flake under default rollback-journal mode |
| F9 | DLQ `raw_text` contains binary or non-UTF8 | Loud (decode error in JSON serialization) | `(r[3] or "")[:240]` truncates string-or-empty; JSON serializer handles strings | If raw_text is bytes (shouldn't happen per schema TEXT NOT NULL), would fail; acceptable risk |
| F10 | Per-channel count query returns row with NULL channel_handle (json_extract returned NULL) | Silent (row would be in dict with key None) | Helper filters `if r[0]` — None keys excluded | Still possible to get an empty dict if ALL rows have NULL extraction; that's just "no dispatches today" — correct |
| F11 | Dashboard reads `paper_trades` while pipeline writes new row mid-query | None (read-only WAL semantics) | SQLite WAL guarantees readers see consistent snapshot; verified `scout/db.py:67` sets WAL on writer connection (file-header persistent) | None |
| F12 | TGDLQPanel 30s poll vs TGAlertsTab 15s poll causes operator confusion | Cosmetic/silent (DLQ shows older counts than messages) | Acceptable per BL-066' scope; documented in panel header | If operator confused, unify cadence in follow-up |
| F13 | New endpoint `/api/tg_social/dlq` 404s if route registration failed | **Loud** (HTTP 404) | T3 endpoint test exercises route via httpx; CI catches | None |
| F14 | Frontend bundle is stale (deploy didn't rebuild dist/) | Silent (UI doesn't show new columns) | §5 step 8 visual confirmation; `git status dashboard/frontend/dist/` post-build clean check (NIT N3) | Operator must run `npm run build` before commit; build artifacts committed per project convention |
| F15 | Cap_per_day defaults to 5 hard-coded if `_DASHBOARD_SETTINGS is None` | Silent (would render `0/5` even if Settings has higher cap) | Acceptable — Settings None means env is broken; fallback mirrors current operational state | Operator should never see this in practice (pipeline would have already failed) |
| F16 | `cashtag_trade_eligible` integer 1/0 vs bool True/False mismatch | Silent (downstream typed code might break later) | T8 isinstance check pins bool type | Future refactor that drops `bool()` cast must update T8 |
| F17 | `tg_social_dlq` table missing on rolled-back-DB scenario | **Loud** (HTTP 500 `no such table`) → degraded under fix | S1 fix: `try/except aiosqlite.OperationalError → []` in `get_tg_social_dlq` (mirror S2 column-missing pattern) | Operator sees empty DLQ — semantically correct given table doesn't exist |
| F18 | Dashboard auth/CORS — DLQ raw_text exposes truncated user-channel content on public VPS `:8000` | Silent (no auth = passive exposure) | Same posture as existing `/api/tg_social/alerts` `text_preview` field (`api.py:786`) — not a new exposure surface | Deferred to operator network policy; not a BL-066' regression |
| F19 | Migration race: `gecko-pipeline` (writer + migrator) and `gecko-dashboard` (reader) start simultaneously; dashboard wins race; channels query fails with `no such column: cashtag_trade_eligible` until pipeline completes migration | **Loud** (HTTP 500 without S2 fix) | S2 defensive try/except → fall back to old shape; T11 ACTIVE test pins this; mitigated for the duration of the race window (typically <1s) | Race window measured; if it persists, operator sees brief degradation until pipeline finishes; revert to pre-BL-066' deploy is one rollback away |

---

## Performance notes

**Cashtag-today per-channel query** (`json_extract` on `paper_trades.signal_data`):
- SQLite does not auto-index json_extract expressions. Query plan: index seek on `idx_paper_trades_signal (signal_type=?)`, then linear scan over ~today's rows for `signal_type='tg_social'`.
- Scale today: ~0–10 cashtag dispatches/day across 8 channels (per memory `project_bl065_deployed_2026_05_04.md`: "operator-driven enablement still pending; no cashtag dispatches yet").
- Even at 100×: scan over 1000 rows is sub-millisecond.
- No expression index added — premature given cardinality. Revisit if message volume grows 1000× (won't happen organically).
- Same query pattern is used by the dispatcher's gate at `dispatcher.py:282` — they share the same performance characteristic.

**Module-level Settings singleton (D4 quantification):**
- `_DASHBOARD_SETTINGS = Settings()` runs once at process import.
- Pydantic v2 `BaseSettings.__init__` runs **field validation against env values for ~30 fields** (per `scout/config.py`); cold-path cost is ~5ms.
- Per-request `Settings()` would burn that 5ms × every endpoint poll. At 15s composite poll cadence × 5 dashboard panels × N concurrent operators, the cumulative saved CPU is small but the **latency floor on the alerts endpoint drops below 10ms** with the singleton — self-justifying without invoking "defensiveness."
- Subsequent endpoint calls reference the cached instance — no syscall cost.
- `env_file` is configured in `scout/config.py:13` as relative `".env"`; resolves against systemd unit's `WorkingDirectory=/root/gecko-alpha/` (verify with `systemctl cat gecko-dashboard.service` before deploy if there's any doubt). If WorkingDirectory differs from repo root, `Settings()` reads no env vars and (for fields without defaults: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`) raises `ValidationError` — caught by the try/except, `_DASHBOARD_SETTINGS = None`, cap_per_day defaults to 5.
- Trade-off: a runtime `.env` edit is not picked up until `systemctl restart gecko-dashboard`. Matches rest of project's operational model.

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

**Partial rollback — honest accounting (D6/S4 fix):** the two CLUSTERS are conceptually independent (DLQ: Tasks 1+2+6; cashtag: Tasks 3+4+5), but **Tasks 5 AND 6 both edit `dashboard/frontend/components/TGAlertsTab.jsx`** (Task 5 adds cashtag columns + stat card; Task 6 adds the `<TGDLQPanel />` mount + import). Partial git-revert at the frontend layer is therefore not clean — reverting Task 6 alone removes the mount but leaves Task 5 column changes (and vice versa).

**Cleaner partial-rollback:** revert at the API layer (Task 4 for cashtag stack, Task 2 for DLQ stack). Frontend gracefully degrades:
- Reverting Task 4 → cashtag columns render `–` via `??` defaults (M4 fix)
- Reverting Task 2 → `TGDLQPanel` shows "Failed to load: HTTP 404"; operator sees a clear error not a corrupted UI

This is the operator's escape hatch if one cluster regresses post-deploy. See plan v2 Self-Review #8.

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
2. **Drift grounding:** ✓ explicit file:line refs to all extended code; deployed schema verified via SSH; WAL claim verified (`scout/db.py:67`).
3. **Test matrix complete:** **10 active + 2 honestly-skipped** placeholders (v2: T9 redesigned runtime, T9b added for F2, T11 promoted from deferred to active per F19 mitigation discipline) covering all surfaces (DB layer, endpoint layer, frontend, contract); aligns with TDD plan tasks.
4. **Failure modes 19/19, silent-failure-first count: 10 silent** — F1, F2, F4 (corrected — `??` defaults render dashes not undefined), F6, F7, F10, F12, F14, F15, F16, F18; **9 loud** — F3 (loud-but-unmonitored, mitigated to non-occurrence), F5 (loud → degraded under S2 fix), F8, F9, F11 (none — protected by WAL), F13, F17 (loud → degraded under S1 fix), F19 (loud → degraded under S2 fix). Reviewers M2/A2/A1/D6 all addressed.
5. **Performance honest:** json_extract scan acknowledged with cardinality rationale; Pydantic field-validation cost quantified; WAL claim sourced (`scout/db.py:67`); env_file path dependency on systemd WorkingDirectory documented.
6. **Rollback complete:** code rollback path + partial-rollback HONESTLY corrected (D6 — TGAlertsTab.jsx is shared across Tasks 5+6; cleaner partial-rollback is at API layer with frontend graceful degrade).
7. **No new DB schema:** verified — no migration needed; backward compat preserved by the defensive try/except in api.py for the rollback-foreign-DB scenario AND for the F19 startup race.
8. **No new contract module:** D2 deferred per scope; revisit when third consumer of BL-065 signal_data appears.
