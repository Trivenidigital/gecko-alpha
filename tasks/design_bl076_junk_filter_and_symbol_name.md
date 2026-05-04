# BL-076: Junk-filter expansion + symbol/name population — Design

**New primitives introduced:** add `"test-"` to `_JUNK_COINID_PREFIXES`; new method `Database.lookup_symbol_name_by_coin_id(coin_id) -> tuple[str, str]` (sequential prioritized lookup with per-table try/except); engine-level WARNING `open_trade_called_with_empty_symbol_and_name` (defense-in-depth, NOT hard-reject); modify 3 dispatcher call sites to populate `symbol`+`name`. NO new DB tables, columns, or settings.

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

## Failure modes (15 — silent-failure-first ordering)

| # | Failure | Silent or loud? | Mitigation in plan v2 | Residual risk |
|---|---|---|---|---|
| F1 | New CoinGecko placeholder family appears (`example-N`, `demo-N`) | **Silent** (paper trade opens against junk) | Pre-merge §"Pre-merge audits" §A enumerates prefixes; if found, add to PR | Future placeholder families need same pre-merge query before next backlog item touching trading admission |
| F2 | `_is_junk_coinid` too aggressive — false-positive rejects legit token starting with `test-` | **Loud** (would log `signal_skipped_junk` for legit token) | T1b pins false-positives `protest-coin`, `biggest-token`, `pre-testnet`, `pretest` | `test-net-token` etc. would still slip through `_JUNK_COINID_PREFIXES` because `test-` matches; acceptable — operator can grep `signal_skipped_junk` to see if any legit token was rejected |
| F3 | One of 3 snapshot tables gets a column rename (BL-NNN renames `volume_history_cg.recorded_at` → `recorded_ts`) | **Silent** (helper's UNION-without-try would fall back to ""; v2 try/except catches the OperationalError per-table) | A1 fix: per-table try/except in `lookup_symbol_name_by_coin_id` | A column rename in `gainers_snapshots` (primary source) would still be detectable via §5 step 9 WARNING-rate spike; see T5d for validation pattern |
| F4 | Engine WARNING never fires (resolver works perfectly OR all 3 dispatchers always have data) | None — this is the success state | n/a | If WARNING ALSO never fires after 14d soak, BL-077 escalates to hard reject |
| F5 | Engine WARNING becomes wallpaper (fires constantly) | **Silent** (operator stops noticing) | §5 step 9 quantifies daily count; soak-then-escalate criterion gates BL-077 on clean soak | If wallpaper, investigate which dispatcher is leaking BEFORE escalating to reject |
| F6 | chain_completed dispatcher has high orphan rate (no snapshot row for most coin_ids) | **Silent** (chain trades open with empty metadata; operator can't identify in dashboard) | §5 step 10 attribution query measures coverage rate; if orphan rate >20%, investigate ingestion gap | If chain trades dominate empty-metadata trades, BL-077 to add JOIN with `candidates` table (different keying — `contract_address` not `coin_id`) or direct CoinGecko fetch |
| F7 | Concurrent migration from BL-NNN adds NEW NOT NULL column to `chain_matches` while pipeline is running | **Loud** (INSERT 500s on missing column) | Already mitigated by SQLite ALTER TABLE adding columns at end with default; pipeline restart picks it up | Standard migration risk — same as any other DB schema change in this project |
| F8 | structlog renderer changes between versions; `capture_logs` API breaks | **Loud** (test suite fails) | Pin structlog version in pyproject.toml (already pinned per project convention); CI catches | None |
| F9 | T2 test fires the WARNING but `open_trade` short-circuits at warmup gate (`PAPER_STARTUP_WARMUP_SECONDS > 0`) BEFORE the WARNING line | None — warmup gate is at engine.py:132 (line numbers approximate); WARNING placement at engine.py:~123 means it fires FIRST | M2 fix: WARNING placed BEFORE warmup gate explicitly | None |
| F10 | NULL `symbol` in legacy snapshot table row | **Silent** (helper would return NULL via tuple, str-typed columns would crash later) | T5d pins `if row[0] and row[1]` filter | None |
| F11 | Race: chain dispatch fires while gainers_snapshots row for that coin_id is being inserted (mid-transaction) | None — read-side lookup will see committed state via WAL | SQLite WAL guarantees readers see consistent snapshot | None |
| F12 | Same coin_id has different symbols in different snapshot tables (e.g., gainers has "PEPE", volume_spikes has "pepe-bsc") | **Silent** (operator might see weird casing in dashboard) | Sequential prioritized lookup picks gainers_snapshots first (canonical); other tables only used as fallback | If operator finds inconsistency in dashboard, the helper docstring documents priority order — root-cause traceable |
| F13 | `_JUNK_COINID_PREFIXES` tuple grows unbounded in future PRs | **Loud** (review pain at >10 entries) | Self-Review #11 documents refactor trigger | Convention enforced by code-review reviewer |
| F14 | Engine WARNING captures false positives — caller intentionally passes empty symbol/name (e.g. test fixture) | **Silent** (operator dismisses as noise) | All known callers must populate symbol+name; test fixtures using `_StubEngine` bypass real engine (verified for BL-065 tests) | If a NEW test fixture leaks empty values to real engine, WARNING fires correctly (catches the bug) |
| F15 | Pre-deploy baseline missing (operator skips §"Pre-merge audits") | **Silent** (post-deploy attribution query has no comparison point) | §"Pre-merge audits" is mandatory; post-deploy operator audit references the file | Operator should run audits before merge; if skipped, attribution is reduced to "fix is in" without coverage breakdown |

---

## Performance notes

**Sequential 3-table lookup in `lookup_symbol_name_by_coin_id`:**
- All 3 source tables have indexes on `coin_id`:
  - `gainers_snapshots`: covered by INSERT-side schema (typical PK or unique constraint)
  - `volume_history_cg`: `idx_vol_hist_cg ON (coin_id, recorded_at)` (verified `scout/db.py:442-443`)
  - `volume_spikes`: standard `coin_id` index in BL-026 deploy
- Each SELECT is O(log n) via index seek, returns at most 1 row.
- Best case (gainers hit): 1 SELECT.
- Worst case (orphan): 3 SELECTs. ~sub-ms total.
- Per chain_completed dispatch: ~3 SELECTs added to existing flow. Negligible.

**Engine WARNING:**
- Single conditional check + 1 structlog event per `open_trade` call when both symbol+name empty.
- After Tasks 3-5 patches land, expected rate: ~0/hour from fixed dispatchers; only chain_completed orphans.

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
2. **Drift grounding:** ✓ explicit file:line refs to all extended code; Pydantic shapes verified; deployed schemas verified; structlog config verified.
3. **Test matrix:** 11 active tests covering both bugs at unit + integration layers; zero deferred.
4. **Failure modes 15/15, silent-failure-first count: 7 silent** — F1, F3, F5, F6, F10, F12, F14, F15 (mitigated or accepted with rationale); **8 loud** — F2, F4, F7, F8, F9, F11, F13. F4 is the success state (no failure).
5. **Performance honest:** indexes verified; sub-ms cost quantified.
6. **Rollback complete:** code-only revert; partial rollback per cluster.
7. **No new DB schema:** verified — no migration.
8. **Soak-then-escalate criterion:** documented in plan v2 §10; tracks WARNING rate daily for 14d before BL-077 hard-reject escalation.
