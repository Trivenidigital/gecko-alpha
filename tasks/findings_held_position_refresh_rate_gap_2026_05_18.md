**New primitives introduced:** NONE. (Implementation primitives are listed in `tasks/plan_held_position_refresh_rate_gap.md`.)

# BL-NEW-HELD-POSITION-REFRESH-RATE-GAP — Findings 2026-05-18

**Source:** srilu prod scout.db; worktree HEAD = `cdeb31f` = origin/master.

## TL;DR

14% silent miss-rate on held-position price refresh confirmed (21/148 open paper_trades with `price_cache` rows > 24h stale). **Root cause empirically confirmed as stale-source behavior**: the 21 stale tokens are CG-lane-EXCLUSIVE (0/21 in gainers_snapshots or trending_snapshots over last 24h despite 4617+645 total entries). The held-position lane IS firing — 127/148 are fresh — but CoinGecko `/simple/price` returns no data for these specific 21 tokens, so their cache rows freeze. Most plausible explanation: tokens delisted from CG free tier OR renamed in CG OR fell out of CG's active set.

**Ship**: visibility-first (gauge + per-token WARN) so operator confirms the per-token list + decides per-token action. **Defer**: `/coins/{id}` fallback (Task 4 descoped pending empirical verification — CG returned HTTP 429 to direct curl during this audit; can't confirm the fallback would recover any tokens).

## Diagnosis evidence

### Stale opens — current state

| Bucket | Count |
|---|---|
| 24-48h stale | 3 |
| 48-72h stale | 10 |
| 72-168h stale | 8 |
| **Total stale > 24h** | **21** |
| Fresh ≤ 24h | 127 |
| Total opens | 148 |

### Per-signal breakdown

| signal_type | n_stale |
|---|---|
| gainers_early | 11 |
| narrative_prediction | 5 |
| losers_contrarian | 3 |
| chain_completed | 2 |

### Per-token detail (top 21)

| symbol | token_id | hours_stale | cache_last |
|---|---|---|---|
| (na) | pythia | 132.9 | 2026-05-12T12:58Z |
| (na) | argentine-football-association-fan-token | 132.9 | 2026-05-12T12:58Z |
| $fartboy | fartboy | 121.3 | 2026-05-13T00:35Z |
| IAG | iagon | 100.9 | 2026-05-13T21:01Z |
| kekius | kekius-maximus | 97.8 | 2026-05-14T00:08Z |
| scrt | secret | 97.2 | 2026-05-14T00:39Z |
| NAVX | navi | 92.6 | 2026-05-14T05:15Z |
| PROM | prometeus | 73.2 | 2026-05-15T00:40Z |
| READY | ready | 71.8 | 2026-05-15T02:05Z |
| AIO | olaxbt | 69.3 | 2026-05-15T04:36Z |
| MAPO | marcopolo | 69.2 | 2026-05-15T04:41Z |
| SAFE | safecoin | 67.4 | 2026-05-15T06:29Z |
| KNTQ | kinetiq | 61.6 | 2026-05-15T12:17Z |
| anthropic | anthropic-prestocks-2 | 60.3 | 2026-05-15T13:38Z |
| BTY | bityuan | 58.8 | 2026-05-15T15:05Z |
| manyu | manyu-2 | 58.2 | 2026-05-15T15:44Z |
| MHORSE | meme-horse | 56.3 | 2026-05-15T17:33Z |
| HP | hippo-protocol | 49.6 | 2026-05-16T00:20Z |
| GRND | superwalk | 40.7 | 2026-05-16T09:14Z |
| CRCLON | circle-internet-group-ondo-tokenized-stock | 32.2 | 2026-05-16T17:44Z |
| FOLKS | folks | 29.7 | 2026-05-16T20:14Z |

### Critical lane-exclusivity check (R1 #1 fold)

| Stale token | gainers_snapshots last 24h | trending_snapshots last 24h |
|---|---|---|
| ALL 21 | **0** | **0** |
| Other tokens in those tables | 4617 | 645 |

This empirically rules out the alternative "rate-limit-truncation" hypothesis (which would imply OTHER lanes are also missing these tokens for an unrelated reason). The 21 are CG-lane-exclusive AND consistently missing.

### Direct CG endpoint test

```bash
curl https://api.coingecko.com/api/v3/coins/pythia?... → HTTP 429 "Throttled"
```

CG free-tier rate limit blocked direct verification. The `/coins/{id}` fallback hypothesis (Task 4) is **unverified** until a non-rate-limited verification window allows manual-curl of ≥1 of the 21 stale tokens.

## Hypothesis elimination matrix (per operator's enumerated list)

| Hypothesis | Empirical signal | Verdict |
|---|---|---|
| Refresh interval too long | Interval=1 cycle (minimum); 127/148 fresh ≤24h | RULED OUT |
| LIFO/FIFO ordering starvation | Lane uses `SELECT DISTINCT`; no ordering | RULED OUT |
| Rate-limiter contention | Would affect ALL tokens equally; 85% fresh | RULED OUT |
| Token filtering (`_is_cg_coin_id`) | Stale tokens ARE CG-shaped per heuristic | RULED OUT |
| Failed writes | Would be random across days, not same-21 | RULED OUT |
| **Stale-source (CG returns empty)** | **0/21 in other CG surfaces; cache_last consistent across days** | **CONFIRMED (most likely)** |

## What this PR ships

### Code changes (`scout/ingestion/held_position_prices.py` + `scout/config.py`)

1. **`_get_cached_price_ages(db, coin_ids)` helper** — direct SQL on `price_cache.updated_at` (avoids touching `db.py`). Returns tz-aware datetimes; missing coins absent from result dict.

2. **`stale_open_count` + `stale_open_pct` gauge** in existing `held_position_refresh_summary` structlog event. Computed against the `held_ids` cohort (NOT just refreshed tokens — captures the silent-miss case). Wrapped in own try/except so failure doesn't block existing log emission.

3. **`held_position_token_persistently_stale` per-token WARN** with 24h in-memory dedup. Emits once per token per 24h window when cache age ≥ `HELD_POSITION_STALE_WARN_HOURS` (default 24). Resets on pipeline restart (acceptable per `feedback_in_memory_telemetry_persistence.md`).

4. **`_reset_warned_today_for_tests()` helper** mirroring existing `_reset_cycle_counter_for_tests()` pattern.

5. **1 new `Settings` key**: `HELD_POSITION_STALE_WARN_HOURS: int = 24` with `_validate_held_position_stale_warn_hours` field_validator (>= 1).

### Tests (`tests/test_held_position_prices.py`)

6 new tests using `structlog.testing.capture_logs()` for proper structlog isolation:

- `test_get_cached_price_ages_returns_aware_datetimes` — helper happy path
- `test_get_cached_price_ages_empty_input` — empty-input early-return
- `test_held_position_settings_default_warn_hours` — default 24
- `test_held_position_settings_warn_hours_validator` — validator rejects 0
- `test_stale_open_count_gauge_in_summary_log` — gauge presence + accuracy (seeds 2 fresh + 1 stale + 1 no-cache; asserts stale_open_count=2, stale_open_pct=50.0)
- `test_persistently_stale_token_emits_warn_once_per_day` — dedup verified (2 consecutive refreshes; assert exactly 1 WARN)
- `test_stale_count_failure_does_not_block_summary_log` — sabotage `_get_cached_price_ages` to raise; assert summary log still emits with `stale_open_count=None`

**27/27 tests pass on srilu Python 3.12.3 / pytest 8.4.2** (existing 21 + 6 new).

## Post-deploy soak plan

After PR merge + operator deploys (`git pull && systemctl restart gecko-pipeline`), operator should:

1. **Confirm gauge fires** — `journalctl -u gecko-pipeline --since "1 hour ago" | grep stale_open_count` should show non-null integer values per pipeline cycle.

2. **Capture per-token WARN list** (24h post-deploy):

```bash
ssh srilu-vps 'journalctl -u gecko-pipeline --since "24 hours ago" | grep held_position_token_persistently_stale | grep -o "token_id=\\S*" | sort -u > /tmp/persistently_stale.txt'
```

3. **Verify list overlap** with the 21 known stale tokens above. If overlap > 80%, the diagnosis is confirmed; promote `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` to PROPOSED for the next cycle.

4. **Alternate-diagnosis fallback (R1 #11 fold):** if post-soak WARN list shows > 25% turnover (different token_ids than the 21), the stale-source hypothesis is falsified; re-investigate rate-limit-mid-batch via `cg_429_backoff` log counts.

5. **Once CG rate-limit subsides**, operator manual-curls `/coins/pythia`, `/coins/iagon`, `/coins/kekius-maximus` (3 of the 21) to confirm whether `/coins/{id}` returns data when `/simple/price` doesn't. Result drives the `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` decision (ship if recovers, skip if also empty).

## Follow-up backlog (filed)

- **`BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT`** (evidence-gated): ship `/coins/{id}` fallback once empirically verified to recover ≥1 of the 21 stale tokens. Includes per-cycle cap (≤5 calls) + `coingecko_limiter.is_backing_off()` check (R2 #3 fold).

- **`BL-NEW-HELD-POSITION-STALE-COUNT-ALERT`** (baseline-first): threshold-driven curl-direct TG alert on `stale_open_count > max(5, 0.05 * held_total)` for ≥3 consecutive cycles. File once baseline is measured (~7d post-deploy) so the threshold is grounded in empirics, not guesswork.

## Cross-references

- `backlog.md` BL-NEW-HELD-POSITION-REFRESH-RATE-GAP (originating; flip to PR-OPEN/SCRIPT-READY at PR open)
- 2026-05-18 cycle-12 PR #157 (BL-NEW-DEX-PRICE-COVERAGE audit — surfaced this gap as a SEPARATE bug from DEX-coverage)
- BL-NEW-HELD-POSITION-REFRESH 2026-05-12 (the originating lane; PR #112)
- `tasks/findings_open_position_price_freshness_2026_05_12.md` (originating triage)
- Memory: `feedback_in_memory_telemetry_persistence.md` (in-memory `_warned_today` is acceptable per restart cadence)
- CLAUDE.md §9c (lever-vs-data-path: the lane IS the lever; CG response IS the data-path; this is data-path silent-skip)
- CLAUDE.md §12a (residual: gauge requires operator-grep; mitigated by per-token WARN which is more visible in journalctl; threshold alert deferred to BL-NEW-HELD-POSITION-STALE-COUNT-ALERT)
