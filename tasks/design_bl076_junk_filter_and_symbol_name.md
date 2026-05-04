# BL-076: Junk-filter expansion + symbol/name population — Design

**New primitives introduced:** add `"test-"` to `_JUNK_COINID_PREFIXES`; new method `Database.lookup_symbol_name_by_coin_id(coin_id) -> tuple[str, str]` (sequential prioritized lookup with per-table `except aiosqlite.OperationalError`); engine-level WARNING `open_trade_called_with_empty_symbol_and_name` AND parallel `log.info` event `trade_metadata_empty` (matches existing `signal_skipped_*` telemetry pattern at `signals.py:34/132/...` so existing operator dashboards aggregate it; future BL-077 hard-reject path becomes "log+return None" using same event name); modify 3 dispatcher call sites to populate `symbol`+`name`. NO new DB tables, columns, or settings.

**v2 changes from 2-agent design-review feedback:**

*MUST-FIX (consensus or high-impact):*
- **M1 (a6fcf0f7) — performance claim unsupported:** rewrote design line 97 — chain_completed orphan rate is UNKNOWN until §5 step 10 measures it post-deploy. Removed bare "expected ~0/hour" prediction. Coupled fix: amended Self-Review #8 — soak-then-escalate is contingent on chain orphan rate; if orphans dominate, BL-077 must add fallback resolution BEFORE escalation.
- **M2 (a6fcf0f7) — F2 wording inverted:** `test-net-token` STARTS WITH `test-`, so it's REJECTED (false positive risk), NOT "slips through". Rewrote F2 to describe the actual risk: legit testnet-themed tokens get rejected; operator can grep `signal_skipped_junk` to spot losses.
- **A3 (a2616834) — skip-counter telemetry channel bypassed:** added parallel `log.info("trade_metadata_empty", reason=..., token_id=..., signal_type=..., signal_combo=...)` next to the WARNING. Existing operator dashboards aggregate `signal_skipped_*` events; WARNING-only would be invisible there. Future BL-077 flips from `log + proceed` to `log + return None` — same event name, additive change.
- **A4 (a2616834) — BL-077 sketch:** added one paragraph to design §"Soak-then-escalate" — BL-077 converts the WARNING block to `log.info("trade_skipped_empty_metadata", ...) ; return None` (mirrors `trade_skipped_warmup` / `trade_skipped_no_price` / `trade_skipped_signal_disabled` at engine.py:136-168). NOT an exception — exceptions break the dispatcher loop's per-signal isolation.

*SHOULD-FIX (worth applying):*
- **a6fcf0f7 S3 — M2 has no test:** added T2b — `test_open_trade_warning_fires_even_during_warmup` with `PAPER_STARTUP_WARMUP_SECONDS=10` monkeypatch. Pins WARNING placement before warmup gate.
- **a6fcf0f7 S4 — Self-Review failure-mode count wrong:** F4 reframed (per A5 below); count corrected.
- **a6fcf0f7 S5 — chain_patterns FK dependency:** added test-matrix annotation referencing `_seed_chain_pattern` helper for future test authors.
- **a6fcf0f7 S6 — F11 WAL claim irrelevant:** rewrote — same-connection reads via `self._conn` are serialized by aiosqlite's single-thread executor; race is structurally impossible, not just isolated by WAL.
- **a6fcf0f7 S7 — F14 forward guard:** documented FakeEngine stub pattern recommendation.
- **a6fcf0f7 S8 — "no DB schema changes" wording:** clarified — "no DDL changes (zero CREATE/ALTER/DROP, zero migrations); new `Database.lookup_symbol_name_by_coin_id` method is pure code".
- **A2 (a2616834) — F8 in-line citation:** replaced "(already pinned per project convention)" with "(pinned `structlog>=24.1,<25` at `pyproject.toml:12`)".
- **A5 (a2616834) — F4 reframe:** F4 now "Engine WARNING fires unexpectedly often (>10/hour) — investigation trigger — root cause: a 4th dispatcher leaks empty metadata that audit didn't catch". Real failure mode (silent: operator dismisses as expected noise; mitigation: §5 step 9 quantifies).
- **A6 (a2616834) — performance scaling math:** added per-LEARN-cycle scaling (N × ≤3 SELECTs ≤ 150/cycle at observed N=10–50, <1ms/cycle DB cost; revisit at N>500).
- **A7 (a2616834) — WARNING caller-context:** added `signal_combo` field to both WARNING and `log.info` event so operator can grep by combo to identify leaking dispatcher.
- **A8 (a2616834) — coupling to 3 hard-coded snapshot tables:** added Self-Review #11 trigger documentation: refactor to `MetadataSource` plugin pattern when (a) 4th source added, OR (b) source priority becomes dynamic per-chain.
- **A9 (a2616834) — F7 doesn't belong:** deleted (project-wide migration risk, not BL-076-specific).
- **A11 (a2616834) — narrow exception catches:** `except Exception:  # noqa: BLE001` → `except aiosqlite.OperationalError:` per-table. Added T5g monkeypatch test asserting non-OperationalError errors propagate (not swallowed).
- **A10 (a2616834) — F16/F17 added:**
  - F16: `lookup_symbol_name_by_coin_id` called with empty/None coin_id → defensive `if not coin_id: return "", ""` at top of helper
  - F17: SQLite locked during sequential SELECTs → already mitigated by per-table `except aiosqlite.OperationalError`; acknowledge as deliberate

*NIT:*
- a6fcf0f7 #10: redundant NULL filter for gainers_snapshots — kept for symmetry across 3 SELECTs.
- a6fcf0f7 #11: gainers index citation cleanup — applied to Performance section.
- a6fcf0f7 #12: plan/design test count mismatch — both now agree on 12 active tests (was 11 + new T2b + T5g).
- a2616834 #9 F7 deletion — applied above.

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Token slug blacklist / placeholder detection | None | Build inline (extend tuple) |
| CoinGecko junk-token filtering | None | Build inline (project-owned) |
| Symbol/name validation for crypto tokens | None | Build inline (engine-level guard) |
| Wash-trade / fraud detection at admission | None | Build inline (extend PR #44 pattern) |

**Awesome-hermes-agent ecosystem check:** No relevant repos.

**Verdict:** Pure project-internal trading-admission filter. Building inline.

---

## Drift grounding (per alignment doc Part 3)

**Read before drafting (verified):**
- `scout/trading/signals.py:565-588` — `_JUNK_COINID_PREFIXES` tuple + `_is_junk_coinid` helper
- `scout/trading/signals.py:591-622` — `_is_tradeable_candidate` (rejects empty ticker, non-ASCII, junk coin_id; doesn't check symbol/name)
- `scout/trading/signals.py:22-75` (volume_spikes), `:625-757` (predictions), `:760-840` (chain_completions) — 3 buggy dispatchers
- `scout/trading/signals.py:78-187` (gainers) — reference pattern that correctly passes symbol+name
- `scout/trading/engine.py:103-115` — `open_trade(symbol="", name="", ...)` defaults
- `scout/spikes/models.py:14-15` (VolumeSpike: symbol+name) and `scout/narrative/models.py:50-51` (NarrativePrediction: symbol+name) — both Pydantic models carry the data
- `scout/db.py:332-348` — `chain_matches` schema: NOT NULL on `steps_matched`, `total_steps`, `anchor_time`, `chain_duration_hours`, `conviction_boost` (M3 verification)
- `scout/db.py:431` (volume_history_cg: symbol+name+recorded_at) and `:445` (volume_spikes: symbol+name+detected_at) and `gainers_snapshots` (symbol+name+snapshot_at) — 3 lookup sources
- `scout/main.py:903-911` — structlog uses `PrintLoggerFactory()` (M1 verification)

**Bug evidence (prod audit 2026-05-04):**
- 2 `test-3` paper trades (#980 first_signal -$9.96, #1551 volume_spike +$188.91)
- ~150+ paper trades with empty `symbol` AND empty `name` across `narrative_prediction` / `volume_spike` / `chain_completed`

---

## Test matrix

| ID | Test | Layer | What it pins |
|---|---|---|---|
| T1 | `test_is_junk_coinid_rejects_test_prefix` | Unit (helper) | Bug 1 fix: `test-3`/`test-1`/`test-99`/`test-coin` all rejected |
| T1b | `test_is_junk_coinid_does_not_overreach_on_test_substrings` | Unit (helper) | Anchored prefix — `protest-coin`, `biggest-token`, `pre-testnet`, `pretest` all pass |
| T2 | `test_open_trade_logs_warning_when_symbol_and_name_both_empty` | Unit (engine) | Defense-in-depth WARNING fires; uses `structlog.testing.capture_logs` (M1) |
| T3 | `test_trade_volume_spikes_passes_symbol_and_name_to_engine` | Unit (dispatcher) | volume_spike dispatcher wires symbol+name through to engine |
| T4 | `test_trade_predictions_passes_symbol_and_name_to_engine` | Unit (dispatcher) | narrative_prediction dispatcher wires symbol+name through to engine |
| T5 | `test_lookup_symbol_name_prefers_gainers_snapshots` | Unit (DB resolver) | Sequential lookup picks gainers_snapshots first (most authoritative) |
| T5b | `test_lookup_symbol_name_falls_through_to_volume_history_cg` | Unit (DB resolver) | Sequential fallthrough chain (gainers → vh → spikes) |
| T5c | `test_lookup_symbol_name_returns_empty_when_no_source_has_row` | Unit (DB resolver) | Orphan coin returns ("","") — caller decides |
| T5d | `test_lookup_symbol_name_skips_null_symbol_in_source` | Unit (DB resolver) | NULL-symbol legacy row filtered by helper's `if row[0] and row[1]` |
| T5e | `test_trade_chain_completions_uses_lookup_helper_for_metadata` | Integration | chain_completed dispatcher uses Database resolver + chain_matches NOT NULL columns satisfied (M3) |
| T5f | `test_trade_chain_completions_falls_back_to_empty_when_no_snapshot` | Integration | Orphan chain dispatch: empty symbol/name + `chain_completed_no_metadata` log fires (uses `capture_logs`) |

**Build-phase deferred tests (none).** All 11 tests are required for build phase per the design's silent-failure-first discipline.

---

## Failure modes (16 — silent-failure-first ordering; F7 deleted, F4 reframed, F16-F17 added)

| # | Failure | Silent or loud? | Mitigation in plan v3 / design v2 | Residual risk |
|---|---|---|---|---|
| F1 | New CoinGecko placeholder family appears (`example-N`, `demo-N`) | **Silent** (paper trade opens against junk) | Pre-merge §"Pre-merge audits" §A enumerates prefixes; if found, add to PR | One-shot human task; recommend quarterly cron of pre-merge §A query as part of BL-077+ housekeeping. Acceptable until placeholder churn outpaces our cadence |
| F2 | `_is_junk_coinid` REJECTS legit testnet-themed token (e.g. `test-net-token`) because `test-` is a prefix match | **Loud** (`signal_skipped_junk` event fires for legit token) | Accepted risk — current `_JUNK_COINID_PREFIXES` uses `startswith` so `test-net-token` IS rejected | Operator can grep `signal_skipped_junk` events to spot any legit token loss. If a legit `test-`-prefixed token surfaces, switch to substring/regex shape per Self-Review #11 trigger |
| F3 | One of 3 snapshot tables gets a column rename (BL-NNN renames `volume_history_cg.recorded_at` → `recorded_ts`) | **Silent** (helper's UNION-without-try would fall back to ""; v2 per-table `except aiosqlite.OperationalError` only fails ONE lookup, others continue) | A1 fix: per-table `except aiosqlite.OperationalError` in `lookup_symbol_name_by_coin_id` (NOT bare `except Exception` — A11 narrowing) | A column rename in `gainers_snapshots` (primary source) would still be detectable via §5 step 9 WARNING-rate spike; see T5b for validation pattern |
| F4 | Engine WARNING fires unexpectedly often (>10/hour) AFTER Tasks 3-5 patches deploy (where dispatchers were supposed to have populated metadata) | **Silent** (operator dismisses as expected noise) | §5 step 9 quantifies daily count; spike triggers root-cause investigation: a 4th dispatcher is leaking empty metadata that pre-deploy audit didn't catch | Investigation surface is the WARNING's `signal_combo` field (A7 fix) — operator greps by combo to identify the unknown leaker |
| F5 | Engine WARNING becomes wallpaper (fires constantly from chain_completed orphans) | **Silent** (operator stops noticing) | §5 step 9 quantifies daily count; soak-then-escalate criterion (Self-Review #8) is contingent on resolving chain_completed coverage FIRST | If wallpaper, BL-077 must add fallback resolution (CoinGecko fetch / candidates JOIN) BEFORE escalating WARNING to hard reject |
| F6 | chain_completed dispatcher has high orphan rate (no snapshot row for most coin_ids) | **Silent** (chain trades open with empty metadata; operator can't identify in dashboard) | §5 step 10 attribution query measures coverage rate post-deploy. **Pre-deploy unknown** (per M1 fix). If orphan rate >20%, investigate ingestion gap | If chain trades dominate empty-metadata trades, BL-077 to add JOIN with `candidates` table (different keying — `contract_address` not `coin_id`) or direct CoinGecko fetch. **This may be the dominant failure mode — measure first, decide later** |
| F8 | structlog renderer changes between versions; `capture_logs` API breaks | **Loud** (test suite fails) | structlog pinned `>=24.1,<25` at `pyproject.toml:12`; CI catches version drift | None |
| F9 | T2 test fires the WARNING but `open_trade` short-circuits at warmup gate (`PAPER_STARTUP_WARMUP_SECONDS > 0`) BEFORE the WARNING line | **Loud** (production WARNINGs vanish during warmup if WARNING placement is wrong) | M2 fix: WARNING placed BEFORE warmup gate explicitly. **T2b** (S3 fix) pins this with `PAPER_STARTUP_WARMUP_SECONDS=10` monkeypatch + assert WARNING fires alongside `trade_skipped_warmup` | None |
| F10 | NULL `symbol` in legacy snapshot table row | **Silent** (helper would return NULL via tuple, str-typed columns would crash later) | T5d pins `if row[0] and row[1]` filter | None |
| F11 | Race: chain dispatch fires while gainers_snapshots row for that coin_id is being inserted (mid-transaction) | None — same-connection reads via `self._conn` are serialized by aiosqlite's single-thread executor | Race is structurally impossible (not just isolated by WAL — single-connection serialization is stronger) | None |
| F12 | Same coin_id has different symbols in different snapshot tables (e.g., gainers has "PEPE", volume_spikes has "pepe-bsc") | **Silent** (operator might see weird casing in dashboard) | Sequential prioritized lookup picks gainers_snapshots first (canonical CoinGecko source); other tables only used as fallback | If operator finds inconsistency in dashboard, the helper docstring documents priority order — root-cause traceable |
| F13 | `_JUNK_COINID_PREFIXES` tuple grows unbounded in future PRs | **Loud** (review pain at >10 entries) | Self-Review #11 documents refactor trigger to settings-backed `PAPER_JUNK_COINID_PREFIXES` | Convention enforced by code-review reviewer |
| F14 | Engine WARNING captures false positives — caller intentionally passes empty symbol/name (e.g. test fixture using real engine) | **Silent** (operator dismisses as noise; signal-to-noise of WARNING erodes) | BL-065 tests use `_StubEngine` (verified `tests/test_bl065_cashtag_dispatch.py:265-268`); future tests should EITHER populate symbol+name OR use a `FakeEngine` stub. WARNING in CI test logs is a feature (catches dispatcher drift), not a bug | Recommendation in `tests/conftest.py` documents the FakeEngine stub pattern; new contributors discover it on copy-paste |
| F15 | Pre-deploy baseline missing (operator skips §"Pre-merge audits") | **Silent** (post-deploy attribution query has no comparison point) | §"Pre-merge audits" is mandatory; post-deploy operator audit references the file | Operator should run audits before merge; if skipped, attribution is reduced to "fix is in" without coverage breakdown |
| F16 | `lookup_symbol_name_by_coin_id` called with empty/None coin_id (caller bug) | **Silent** (would `SELECT ... WHERE coin_id = ''` always-empty result; return ("", "") with no log) | Defensive `if not coin_id: return "", ""` at top of helper | None |
| F17 | SQLite locked during the 3 sequential SELECTs (e.g., concurrent writer holding write lock) | **Loud** (`OperationalError: database is locked`) → **degraded under fix** (per-table `except aiosqlite.OperationalError` returns ("", "") for that lookup, falls through to next table) | A11 fix: per-table catch handles this incidentally as well as schema drift. **Acknowledged as deliberate**, not coincidental | If ALL 3 tables locked simultaneously, returns ("", "") + caller logs `chain_completed_no_metadata` — same path as orphan |

---

## Performance notes

**Sequential 3-table lookup in `lookup_symbol_name_by_coin_id`:**
- All 3 source tables have indexes on `coin_id` (verified):
  - `gainers_snapshots`: `idx_gainers_snap ON (coin_id, snapshot_at)` (`scout/db.py:490-491`)
  - `volume_history_cg`: `idx_vol_hist_cg ON (coin_id, recorded_at)` (`scout/db.py:442-443`)
  - `volume_spikes`: `idx_vol_spikes ON (coin_id, detected_at)` (`scout/db.py:459-460`)
- Each SELECT is O(log n) via index seek, returns at most 1 row.
- Best case (gainers hit): 1 SELECT.
- Worst case (orphan): 3 SELECTs. ~sub-ms total.
- **Per LEARN cycle (5min):** N chain matches × ≤3 SELECTs = ≤3N. At observed N=10–50, <150 SELECTs/cycle = <1ms/cycle DB cost. Re-evaluate if N exceeds 500/cycle (refactor to UNION ALL with per-table OperationalError fallback for happy-path single round-trip).
- **Helper docstring acknowledges trade-off** (per A1 architecture-review): worst case 3 round-trips per orphan; refactor trigger documented at the cardinality threshold.

**Engine WARNING + parallel `log.info` event:**
- Single conditional check + 2 structlog events (WARNING + INFO `trade_metadata_empty`) per `open_trade` call when both symbol+name empty.
- After Tasks 3-5 patches land, **expected rate from fixed dispatchers (volume_spike + narrative_prediction): 0/hour**.
- **chain_completed orphan rate: UNKNOWN until §5 step 10 measures it post-deploy.** If orphan rate is significant, soak-then-escalate criterion (Self-Review #8) is contingent on resolving chain_completed coverage FIRST — see F5/F6.

---

## Rollback

**No DB rollback required.** Zero schema changes; zero migrations. Pure code revert:

```bash
ssh root@89.167.116.187 "cd /root/gecko-alpha && systemctl stop gecko-pipeline && git checkout <prev-master-sha> && find . -name __pycache__ -exec rm -rf {} + && systemctl start gecko-pipeline"
```

Verification post-rollback:
- `journalctl -u gecko-pipeline --since "1 minute ago" | grep "open_trade_called_with_empty_symbol_and_name"` returns empty (WARNING removed).
- Next polling cycle opens trades with empty symbol/name again (regression to pre-BL-076 behavior — expected).
- `test-N` paper trades may resume opening (regression to pre-BL-076 behavior — expected; only 2 in 30 days, low-risk to wait for re-deploy).

**Partial rollback (junk filter only / dispatcher fix only):** the two clusters (junk-filter: Task 1; dispatcher fix: Tasks 2-5) are independent at the file level — Task 1 is a single tuple edit, Tasks 2-5 touch different functions. `git revert <task-1-commit>` reverts ONLY junk filter; same for dispatcher commits. This is operator's escape hatch if one cluster regresses post-deploy.

---

## Operational verification (§5 — see plan v2)

Plan v2 §"Pre-merge audits" + §"Deploy verification" cover:
- Pre-merge: prefix-audit (other placeholder families), symbol/name baseline (signal_type breakdown)
- Pre-deploy: error baseline + WARNING baseline capture
- Stop-FIRST sequence with pycache clear (lesson from BL-066')
- Endpoint reachability NOT applicable (no new endpoints)
- Junk filter ACTIVE (positive path: signal_skipped_junk events for test- coins)
- Symbol/name populated for new trades
- Post-deploy error delta vs baseline
- Engine WARNING rare (not wallpaper) — feeds soak-then-escalate decision
- Symbol/name fix attribution (correlates against pre-deploy baseline)

Design adds no operational verification beyond plan; this section is here for cross-reference.

---

## Self-Review

1. **Hermes-first present:** ✓ table + ecosystem + verdict per convention.
2. **Drift grounding:** ✓ explicit file:line refs to all extended code; Pydantic shapes verified; deployed schemas verified; structlog config verified; pyproject pin cited.
3. **Test matrix:** **12 active tests** (T1-T1b junk filter + T2-T2b engine WARNING + T3-T4 dispatcher wiring + T5-T5g resolver + chain integration + structlog capture across all). Zero deferred. T2b pins WARNING-vs-warmup ordering (S3 fix); T5g pins per-table `except aiosqlite.OperationalError` narrowness (A11 fix).
4. **Failure modes 16/16, silent-failure-first count: 9 silent** (F1, F3, F4, F5, F6, F10, F12, F14, F15, F16) **/ 7 loud** (F2, F8, F9, F11, F13, F17 [degraded under fix], reframed F4 [success-state-with-investigation-trigger]). F7 deleted (project-wide migration risk, not BL-076-specific).
5. **Performance honest:** all 3 indexes verified at exact `scout/db.py` lines; per-LEARN-cycle scaling math added; WARNING rate prediction conservative (`UNKNOWN until measured` for chain_completed).
6. **Rollback complete:** code-only revert; partial rollback per cluster (junk-filter / dispatcher fix independent at file level).
7. **No DDL changes:** verified — zero CREATE/ALTER/DROP, zero migrations. New `Database.lookup_symbol_name_by_coin_id` method is pure code; reverts with the file. (Wording clarification per a6fcf0f7 S8.)
8. **Soak-then-escalate criterion (CONTINGENT, not binary — per A4 sketch):**
   - **Soak target:** 14 consecutive days of zero `trade_metadata_empty` events (the parallel `log.info` event added per A3 — counts in same operator dashboard as `signal_skipped_*`).
   - **Contingent on:** chain_completed orphan rate first reaching ~0/cycle. If F6 dominates the WARNING surface (orphans keep firing), BL-077 must FIRST add fallback resolution (CoinGecko fetch / candidates JOIN) before flipping the WARNING.
   - **BL-077 architecture sketch (per A4):** convert engine WARNING block at `engine.py:~123` from `log.warning(...)` to `log.info("trade_skipped_empty_metadata", ...) ; return None`. Mirrors existing `trade_skipped_warmup` / `trade_skipped_no_price` / `trade_skipped_signal_disabled` patterns at `engine.py:136-168`. **NOT** an exception — exceptions break the dispatcher loop's per-signal isolation. Caller already handles `None` returns from `open_trade`.
9. **Engine WARNING + parallel INFO event channel choice (per A3):** project's existing telemetry pipeline aggregates `signal_skipped_*` / `trade_skipped_*` `log.info` events. WARNING-only would be invisible there. Adding the parallel INFO event matches the existing convention; future BL-077 hard-reject becomes additive (flip from `log.warning + proceed` to `log.info + return None` using the same event name).
10. **F4 reframed as real failure mode (per A5):** "WARNING fires unexpectedly often after Tasks 3-5 deploy → investigation trigger" — surfaces the case where a 4th unknown dispatcher leaks empty metadata. Investigation surface is the `signal_combo` field on the WARNING.
11. **Hard-coded 3 snapshot tables — refactor trigger (per A8):** acceptable at current cardinality. Refactor to `MetadataSource` plugin pattern when EITHER (a) a 4th source is added OR (b) source priority becomes dynamic (e.g., per-chain ordering). One-sentence trigger documented in helper docstring + here for future contributors.
12. **Test fixture FK dependency (per S5):** T5e/T5f require `chain_patterns` row seeded first (FK on `chain_matches.pattern_id`, `db.py:336`). Future chain_matches integration tests must do the same — test matrix annotates this; consider factoring `_seed_chain_pattern(sd, id)` into `tests/conftest.py` if a 3rd test needs it.
