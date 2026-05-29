**New primitives introduced:** NONE

<!--
This is BL-NEW-SIGNAL-EARLY-USEFULNESS-SCORECARD. It is the *narrowing step* the
operator's backlog entry called for: "fold into the Signal Trust roadmap unless a
narrower audit proves it should stand alone." This design builds the standalone,
offline-only audit so the operator can decide whether it stands alone or folds.
No new DB tables, no new writers, no new pipeline stages, no new config keys.

REWRITE 2026-05-29: a two-vector review (structural + product) BLOCKED the prior
draft for asserting data paths that do not exist on disk. Every column referenced
below is now verified against `scout/db.py` (line numbers cited). The changelog
versus the blocked draft is §0.
-->

# Design — Signal Early Tradable Usefulness Scorecard (offline audit)

**BL:** `BL-NEW-SIGNAL-EARLY-USEFULNESS-SCORECARD`
**Status of design:** DRAFT 2026-05-29 (rewrite after BLOCK)
**Script (to be built in a follow-up PR):** `scripts/audit_signal_early_usefulness.py`
**Tests:** `tests/test_audit_signal_early_usefulness.py`
**Reference implementation mirrored:** `scripts/audit_price_path_coverage.py` +
`tests/test_audit_price_path_coverage.py` (conventions copied where applicable:
pure `build_report()` with injected `now`; read-only `mode=ro` sqlite URI;
argparse `main()` returning 0/2; PRAGMA-driven `schema_findings`; UTC `Z`
timestamps; null-out per-group stats below floor; importlib test loading +
`tmp_path` sqlite + `FIXED_NOW`). **Note:** the reference's `_points_distribution`
is `int`-typed with a hardcoded `< min_samples` default of 5 — it is **forked**,
not imported, for float MFE/MAE distributions (see §3.4 + §0 item 12).

---

## 0. Changelog vs the BLOCKED draft (what each review fix did)

| # | Blocked draft asserted | Verified reality (`scout/db.py`) | Fix in this rewrite |
|---|---|---|---|
| 1 | Metric 5 "entered Today's Focus before/after move" from a persisted Today's-Focus table | **No persisted Today's-Focus membership table.** `dashboard/db.py get_todays_focus` recomputes live; only persistence is browser localStorage | RESCOPED to the one persisted proxy `gainers_comparisons.appeared_on_gainers_at` (NOT NULL, db.py:938); renamed **"appeared-on-gainers timing vs local peak"**; AVAILABLE ONLY for the gainers-tracker cohort; all other families emit `unsupported_for_signal` (never a false "not surfaced"/0). Per-signal `metric5_data_path_available` bool in `schema_findings`. §3.2-M5, §3.6 |
| 2 | Metric 4 "venue route at detection" PRAGMA-derivable | **No venue column** on `paper_trades` (db.py:996-1043) or `paper_trade_entry_snapshots` (db.py:3970-3995). `venue_*` tables are the BL-055 live layer keyed by `(venue,symbol)`, not per-paper-trade | DROPPED the venue flag. Documented as **permanently `None`** with reason; a test asserts it is `None`. Not presented as PRAGMA-derivable. §2.3, §3.2-M4, §7 case 2c |
| 3 | Sidecar table named `actionability_entry_snapshot` | Actual name is **`paper_trade_entry_snapshots`** (db.py:3970); PK `paper_trade_id` FK→`paper_trades(id)` | Every reference + PRAGMA probe renamed. Metric-4 facts mapped only to columns that exist: `actionable_at_entry`, `actionability_reason_at_entry`, `liquidity_usd_at_entry` (db.py:3980,3987,3988). §2.3, §3.6 |
| 4 | P0/entry basis ambiguous (R3: detected_price vs first volume point) | `paper_trades.entry_price REAL NOT NULL` (db.py:1005) — universal, one per detection | P0 **defaults to `paper_trades.entry_price` for EVERY signal**. Makes MFE/MAE cross-signal comparable and comparable to paper PnL. `detected_price`/first-volume only documented as fallback that is **essentially unreachable** (NOT NULL → never absent). R3 ambiguity removed. §2.1, §3.2 |
| 5 | `had_fresh_price_at_detection` from `gainers_comparisons.detected_price` | `detected_price` only populated for the gainers cohort → collapses to False/None for chain/momentum/slow_burn | Sourced from `paper_trade_entry_snapshots` presence (cohort-neutral) **first**; gainers `detected_price` only as the gainers-cohort enrichment. Explicit note that detected_price coverage is gainers-only so per-family rates are not mis-read (§9c "no data path ≠ bad signal" trap). §2.3, §3.2-M4 |
| 6 | No corpus/join-rate framing | Two-corpus reality (micro-cap scorer vs CG-markets-watcher) | Per-signal `corpus` tag; metrics read conditional on `volume_history_cg` join rate; `n_joinable`/`n_unjoinable` surfaced next to **every** metric block; warn MFE/MAE only comparable within similar join-rate bands. §2.5, §3.4 |
| 7 | Single n-floor of 5 for all stats | This is a multi-metric ranking claim, not a single coverage number | DISTRIBUTIONAL floor raised to **>=10**; keep 5 only for binary join-rate counts; print `LOW_CONFIDENCE` on any distribution where n<10. §3.0, §3.4 |
| 8 | Repeat detections treated as independent rows (R1 deferred) | `UNIQUE(token_id, signal_type, opened_at)` → repeat fires are distinct rows | DEFAULT: dedup to **earliest** detection per `(token_id, signal_type)` within the cohort for usefulness metrics; multi-fire count reported separately. Do NOT dedup across different signals. §3.3 |
| 9 | Immaturity gated only for MFE | time-to-peak + MAE-before-favorable computed over truncated windows | Per-horizon immaturity gating extended to MFE **and** time-to-peak **and** MAE; add `window_elapsed_fraction`; gate the aggregate. §3.2, §3.3a |
| 10 | `fav_eps` default 0.0 (R5) | microscopic blip wrongly called "favorable" | `fav_eps` pinned to **0.01** (configurable). §3.0, §3.2-M3 |
| 11 | Metric 1 named `time_to_local_peak_minutes`, no edge flag | peak at window edge is edge-censored | Renamed `time_to_peak_within_max_horizon_minutes` + `peak_at_window_edge` bool. §3.2-M1 |
| 12 | Output allow-list test omitted `total_rows`; implied importing `_points_distribution` | reference key set includes `total_rows`; `_points_distribution` is int-typed, hardcoded `<5` | Allow-list test includes `total_rows`. `_points_distribution` **forked** as `_float_distribution(values, *, min_samples)` with configurable float floor — not imported. §3.4, §5, §7 case 12 |

Removed entirely from the blocked draft: reviewer-decisions R1/R3/R5/R6 (now
pinned above, no longer "implementer's choice"); the speculative `had_venue_route`
flag; the "persisted today's-focus rows" join (no such table). R4 (runner-board
source) is retained as a documented non-blocker (§3.2-M1).

---

## Hermes-first analysis

Checked the Hermes skill hub (`hermes-agent.nousresearch.com/docs/skills`) plus
the awesome-hermes-agent ecosystem for any skill that already covers
"signal performance scoring / post-detection time-series usefulness metrics over
a project-local SQLite ledger."

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Signal performance scoring (per signal-family usefulness rollups) | none found — Hermes skills cover agent tool-use / orchestration, not project-internal trade-signal scorecards | build from scratch — gecko-alpha-internal read of `paper_trades` + `volume_history_cg` |
| Time-series usefulness metrics (time-to-peak, MFE, MAE over a price path) | none found — no MFE/MAE / max-favorable-excursion skill in the hub | build from scratch — pure-Python over a sorted point list; no external dep |
| Offline DB audit / read-only diagnostic harness | none found — closest is generic shell/sql tool-use, which adds a runtime boundary this offline script avoids | build from scratch — mirror the in-tree `audit_price_path_coverage.py` |
| n-gated statistical rollup (suppress small-N groups) | none found | build from scratch — reuse the in-tree `len(values) < floor → None` convention |

**awesome-hermes-agent ecosystem check + verdict:** scanned the ecosystem index
for trading-signal-evaluation, MFE/MAE, or SQLite-audit packages; the ecosystem
is oriented toward agent capability plugins (web, code, comms), none of which map
to an offline read-only scorecard over a local trade ledger. **Verdict: none
apply; build from scratch, mirroring the shipped `audit_price_path_coverage.py`
convention to stay consistent with the existing audit family.**

---

## 1. Goal and scope (faithful to spec)

Evaluate each **signal family** (`paper_trades.signal_type`) on **early tradable
usefulness**, not eventual paper PnL. For each detected candidate, the audit
reconstructs the post-detection price path and asks: did the signal surface an
inspectable candidate *before* the move, with enough tradability data to act?

The spec pins these metrics (`.spec_signal_early.txt` / `backlog.md:161-166`).
This rewrite states, per metric, exactly what is buildable on disk:

1. Time from first detection to local peak. **Buildable** from `paper_trades.opened_at`
   + `volume_history_cg`. ("runner-board event" has no canonical persisted ts — §3.2-M1.)
2. Max favorable move within 1h/4h/24h after detection. **Buildable** (MFE).
3. Max adverse move before the favorable move. **Buildable** (MAE).
4. Whether the row had fresh price, ~~venue route~~, liquidity/tradability facts,
   and actionability state at detection. **Partially buildable**: fresh price +
   liquidity + actionability from `paper_trade_entry_snapshots`; **venue route DROPPED**
   (no column exists — §0 item 2).
5. ~~Whether it entered Today's Focus before/after the move.~~ **RESCOPED**: no
   persisted Today's-Focus table exists. Only the gainers cohort has a persisted
   surface ts (`appeared_on_gainers_at`); all other families emit
   `unsupported_for_signal` (§0 item 1, §3.2-M5).

**Anti-scope (verbatim from spec):** offline scorecard only. Do **not** use the
scorecard to auto-rank live rows, auto-enable/disable signals, prune sources, or
send alerts without a separate design and no-lookahead guard.

### What this audit does NOT do (encoded as contract — see §4)
- No writes of any kind (read-only `mode=ro` URI; assert `INSERT/UPDATE/DELETE`
  raise on the connection).
- No network calls. Unlike `audit_price_path_coverage.py` (which fetches the live
  `/api/todays_focus` endpoint), this audit is **purely DB-local**. The metric-5
  surface-timing fact is therefore derived from the only persisted proxy
  (`gainers_comparisons.appeared_on_gainers_at`) and is `unsupported_for_signal`
  for families without a persisted surface ts — never a live HTTP fetch, never an
  inferred zero.
- No ranking output, no alert intent, no signal-enable/disable verdict, no
  threshold tuning. Output is descriptive statistics per signal family only.

---

## 2. Data sources + exact join model (every column verified)

> **Schema-verification convention (mirrors reference):** column existence is
> confirmed at runtime via PRAGMA and surfaced in `schema_findings` (§3.6). If a
> PRAGMA check fails, the dependent metric nulls out (or emits
> `unsupported_for_signal`) rather than crashing — never a false zero. Mirrors the
> reference `_column_exists` / `_table_exists`.

### 2.1 `paper_trades` — the detection cohort (db.py:996-1043)
Verified columns used:
- `id INTEGER PRIMARY KEY` — FK target for the entry-snapshot sidecar join.
- `token_id TEXT NOT NULL` — identifier. **CG-slug caveat** (comment db.py:990-994
  + MEMORY note): `token_id` holds *either* a contract address (chain-sourced rows)
  *or* a CoinGecko slug / `price_cache.coin_id` (CG-sourced rows, `chain='coingecko'`).
  NOT guaranteed on-chain. Joined directly to `volume_history_cg.coin_id`;
  joinable-vs-unjoinable is first-class output (§3.7).
- `symbol TEXT NOT NULL`.
- `signal_type TEXT NOT NULL` — **grouping key for the scorecard.**
- `opened_at TEXT NOT NULL` — UTC ISO; **`t0` for all post-detection metrics.**
  `UNIQUE(token_id, signal_type, opened_at)` ⇒ one row per detection event.
- `chain TEXT NOT NULL` — documents the CG-slug-vs-contract split and feeds the
  `corpus` tag (§2.5).
- **`entry_price REAL NOT NULL` (db.py:1005) — the P0 / entry basis for EVERY
  signal.** Universal (NOT NULL), one value per detection, and identical to the
  basis paper PnL uses → MFE/MAE are cross-signal comparable and comparable to
  paper PnL. This replaces the blocked draft's R3 ambiguity. Fallback to
  `detected_price` / first-volume point is documented but **unreachable** in
  practice: `entry_price` cannot be null per the NOT NULL constraint, so the
  fallback branch can only ever fire on a schema that lacks the column entirely
  (caught by PRAGMA → `schema_findings.paper_trades_has_entry_price=False`).

**`t0` definition:** `t0 = opened_at`. Each row's `opened_at` is its own first
detection for that `(token, signal)` tuple. Repeat fires are distinct rows; the
usefulness metrics dedup to the earliest per `(token_id, signal_type)` (§3.3).

### 2.2 `volume_history_cg` — the post-detection price path (db.py:870-880)
Verified columns `(coin_id TEXT NOT NULL, price REAL, recorded_at TEXT NOT NULL)`;
index `idx_vol_hist_cg(coin_id, recorded_at)` (db.py:881). The writer prunes rows
older than 7 days. Same single source-of-truth the reference audit uses; same
7-day retention ceiling.

- Join key: `volume_history_cg.coin_id = paper_trades.token_id` (direct, same as
  reference). Rows whose `token_id` does not appear in `coin_id` are **unjoinable**
  → reported explicitly, never "no move."
- Price filtering: reuse the reference guard exactly —
  `price IS NOT NULL AND price > 0 AND price < 1e308`.

**Retention consequence (load-bearing):** because `volume_history_cg` keeps only
7 days, the 24h post-detection horizon is fully observable only for detections
recent enough that `t0 + 24h <= now` AND whose points have not aged out. The audit
(a) excludes/marks **immature** windows per-horizon (`t0 + h > now`, no-lookahead),
and (b) reports per-row price-path coverage so "no favorable move" is never
confounded with "no price data retained." §3.3, §3.7.

### 2.3 At-detection fact sources (metric 4) — verified columns only
Metric 4 ("fresh price, ~~venue route~~, liquidity/tradability, actionability
state at detection") maps to facts persisted around detection time. **Verified**
sources:

- **`paper_trade_entry_snapshots`** (db.py:3970-3995) — the durable at-entry
  sidecar (BL actionability-entry-snapshot). PK `paper_trade_id` FK→`paper_trades(id)`.
  **This is the preferred, cohort-neutral source** for metric 4 (covers any
  signal family that wrote a snapshot, not just gainers). Verified columns the
  audit maps:
  - `actionable_at_entry INTEGER` (db.py:3988) → `actionability_state_at_detection`
    (1=actionable / 0=blocked / NULL=unknown).
  - `actionability_reason_at_entry TEXT` (db.py:3987) → reason string when present.
  - `liquidity_usd_at_entry REAL` (db.py:3980) → `had_liquidity_fact_at_detection`
    (True if non-null).
  - Snapshot-row presence itself (`paper_trade_id` joins) → `had_entry_snapshot`
    and is the cohort-neutral basis for `had_fresh_price_at_detection` (§0 item 5).
  - **No venue column exists here** — see venue note below.
- **`gainers_comparisons`** (db.py:932-952) — gainers-cohort enrichment only.
  Verified columns: `coin_id`, `appeared_on_gainers_at TEXT NOT NULL` (db.py:938),
  `detected_price REAL` (db.py:948), `peak_price`, `peak_gain_pct`. A non-null
  `detected_price` joinable to a gainers candidate ⇒ a gainers-cohort fresh-price
  fact. **Coverage is gainers-only** — for chain/momentum/slow_burn families
  `detected_price` is absent, so the audit does NOT read fresh-price from here for
  those families (would mis-read "no data path" as "stale at detection", the §9c
  trap). Cohort-neutral fresh-price comes from snapshot presence above.

**Venue route — DROPPED (verified absent).** There is **no venue column** on
`paper_trades` (db.py:996-1043) or `paper_trade_entry_snapshots` (db.py:3970-3995).
The `venue_*` tables (`venue_overrides` db.py:1816, `venue_health` db.py:2494,
`venue_listings` db.py:2531, `venue_rate_state` db.py:2545,
`signal_venue_correction_count` db.py:2657) are the **BL-055 live-trading layer**,
keyed by `(venue, symbol)` — not per-paper-trade and not at-detection. The audit
emits `had_venue_route_at_detection = None` **permanently**, with this reason in
`schema_findings.venue_route_unsupported_reason`, and a test asserts it is always
`None` (never True/False, never PRAGMA-derived). §7 case 2c.

> Because the snapshot + `gainers_comparisons` column sets are migration-dependent,
> metric 4 flags are derived defensively via PRAGMA + LEFT JOIN: each flag is
> `True` / `False` / `None (column/table absent)`. `None` is reported in
> `schema_findings` and **never** collapsed to `False`.

### 2.4 Tables explicitly NOT counted for the price path
Same posture as the reference: `gainers_snapshots`, `losers_snapshots`,
`momentum_7d`, `slow_burn_candidates`, `volume_spikes` are documented in
`schema_findings.alternate_price_history_tables_present` but **not** used for the
price path. Widening the price source is a separate decision, out of scope.

### 2.5 Corpus tag + join-rate framing (review fix 6)
gecko-alpha runs two corpora (MEMORY: two-corpus architecture):
- **micro-cap scorer corpus** ($10K-$500K) — chain-sourced rows, `token_id` is a
  contract address.
- **CG-markets-watcher corpus** ($10K-$500M) — `slow_burn`/`momentum_7d`/`gainers`,
  `token_id` is a CG slug joinable to `volume_history_cg.coin_id`.

Each signal block carries a `corpus` tag derived from the dominant `chain` /
join behavior of its rows. Because `volume_history_cg` is the CG-watcher's table,
**join rate to `volume_history_cg` differs sharply by corpus** — micro-cap
contract-address rows are largely unjoinable. The audit therefore:
- surfaces `n_joinable` / `n_unjoinable` prominently next to **every** metric
  block (not just at the top level), and
- emits a `comparability_warning` noting that MFE/MAE are only comparable within
  signals of **similar join-rate bands** (a 90%-joinable signal's MFE is not
  comparable to a 10%-joinable signal's MFE — the latter is a coverage artifact,
  not a usefulness difference).

---

## 3. Pure core + per-metric pseudocode

### 3.0 CLI (argparse `main()`)

```
usage: audit_signal_early_usefulness.py
    [--db PATH]                 default: scout.db
    [--horizons "1,4,24"]       comma-separated post-detection horizons in HOURS.
                                default "1,4,24". each parses to positive int,
                                1 <= h <= 168 (7-day volume_history_cg ceiling);
                                non-empty, deduped, sorted ascending.
    [--min-n N]                 BINARY-count floor (join-rate counts). default 5.
                                validation: N >= 1.
    [--min-n-dist N]            DISTRIBUTIONAL floor (MFE/MAE/time-to-peak).
                                default 10 (review fix 7). distributions with
                                fewer than this many values print LOW_CONFIDENCE.
                                validation: N >= 1.
    [--fav-eps F]               favorable threshold for MAE-before-favorable.
                                default 0.01 (review fix 10). validation: F >= 0.
    [--lookback-days D]         cohort window: detections with
                                opened_at >= now - D days. default 7 (matches
                                retention; >7 allowed but flags
                                lookback_exceeds_retention). validation: 1<=D<=90.
    [--no-dedup]                disable intra-(token,signal) earliest-fire dedup
                                (default ON, review fix 8). multi-fire count is
                                reported either way.
    [--json]                    emit machine JSON instead of human text.
```

**Exit codes (mirror reference):**
- `0` — report produced (even if every signal is INSUFFICIENT_DATA).
- `2` — argument validation failure (`stage:"args"`) or DB open failure
  (`stage:"db_open"`). **No `stage:"fetch"`** — no network call. Error payload
  identical to reference: `{"status":"error","stage":"...","error":"..."}` to
  stdout when `--json`, else stderr.

`now = datetime.now(timezone.utc)` computed in `main()` and **injected** into
`build_report(...)` so tests pass `FIXED_NOW`.

### 3.1 Pure core signature

```python
def build_report(
    conn: sqlite3.Connection,
    horizons_h: list[int],     # e.g. [1, 4, 24]
    min_n: int,                # binary-count floor, default 5
    min_n_dist: int,           # distributional floor, default 10
    fav_eps: float,            # favorable threshold, default 0.01
    lookback_days: int,        # cohort window
    dedup: bool,               # earliest-fire dedup per (token,signal), default True
    now: datetime,             # injected for determinism
) -> dict[str, Any]:
    ...
```

No HTTP, no `urllib`. Only I/O is read-only SQL on `conn`.

### 3.2 Per-row metric computation

For each surviving cohort row (`opened_at >= now - lookback_days`, after dedup §3.3):

```
t0          = parse_iso(opened_at)              # detection time
P0          = entry_price                       # paper_trades.entry_price (NOT NULL)
              # fallback (unreachable): gainers_comparisons.detected_price,
              # else first observed volume_history_cg point. See §2.1.
max_h       = max(horizons_h)
cutoff_hi   = min(t0 + max_h hours, now)        # cap at now: no-lookahead

path = SELECT price, recorded_at
       FROM volume_history_cg
       WHERE coin_id = :token_id
         AND recorded_at >= :t0_iso
         AND recorded_at <= :cutoff_hi_iso
         AND price IS NOT NULL AND price > 0 AND price < 1e308
       ORDER BY recorded_at ASC

joinable    = len(path) > 0
# returns are signed fractions relative to P0: (p - P0)/P0
```

If `path` is empty the row is **unjoinable**: all move metrics `None`, counted in
the unjoinable bucket (NOT "no move").

**Metric 1 — `time_to_peak_within_max_horizon_minutes` (+ `peak_at_window_edge`)**
(review fix 11)
```
window = points in path with recorded_at <= t0 + max_h   # full observed window
if window empty: time_to_peak_... = None
else:
  peak_point = argmax(price) over window
  time_to_peak_within_max_horizon_minutes = minutes(peak_point.recorded_at - t0)
  peak_at_window_edge = (peak_point is the last point AND window not yet mature)
        # edge-censored: true peak may lie beyond the observed window
  # only included in the aggregate if the max-horizon window is MATURE
  #   (t0 + max_h <= now). Immature → excluded from the time-to-peak aggregate
  #   (review fix 9) and counted in time_to_peak_immature_excluded.
# "runner-board event" (spec wording): no canonical persisted runner-board
#   timestamp exists in scout/db.py, so local peak over volume_history_cg is the
#   pinned definition. Documented, not silently substituted. (Former R4.)
```

**Metric 2 — `mfe_within_{h}h` (Max Favorable Excursion), one per horizon**
```
for h in horizons_h:
    win = points in path with recorded_at <= t0 + h hours
    mature_{h}h = (t0 + h hours) <= now            # no-lookahead maturity flag
    window_elapsed_fraction_{h}h = clamp((now - t0)/(h hours), 0, 1)
    if win empty: mfe_{h}h = None                  # no data in this horizon
    else: mfe_{h}h = max( (p.price - P0)/P0 for p in win )   # can be 0 if never rose
    # immature horizon (mature_{h}h False) → mfe_{h}h EXCLUDED from horizon-h
    #   aggregate by default; counted in immature_excluded_{h}h. Never silently
    #   counted as a complete observation.
```

**Metric 3 — `mae_before_favorable` (Max Adverse Excursion before favorable)**
(review fixes 9 + 10)
```
# favorable = first point with return > fav_eps (default 0.01) — a microscopic
#   blip is NOT favorable.
favorable_idx = first index in path where (p.price - P0)/P0 > fav_eps
if favorable_idx is None:
    mae_before_favorable = worst drawdown over the entire observed window
    favorable_reached = False
else:
    pre = path[0 : favorable_idx]
    mae_before_favorable = min((p.price - P0)/P0 for p in pre) if pre else 0.0
    favorable_reached = True
# mae <= 0; 0.0 = no adverse excursion before going favorable.
# GATED on max-horizon maturity like time-to-peak: rows whose max-horizon window
#   is immature are excluded from the mae aggregate (review fix 9), counted in
#   mae_immature_excluded. Do NOT mix truncated-window mae with mature rows.
```

**Metric 4 — at-detection fact flags (booleans, defensive; review fixes 2,3,5)**
```
had_entry_snapshot              : True if a paper_trade_entry_snapshots row joins
                                  on paper_trade_id; None if table absent.
had_fresh_price_at_detection    : COHORT-NEUTRAL — True if had_entry_snapshot
                                  (snapshot captured at entry implies a price was
                                  observed). For the gainers cohort additionally
                                  corroborated by non-null gainers_comparisons.detected_price.
                                  NOT read from detected_price for non-gainers
                                  families (gainers-only coverage; §2.3).
had_venue_route_at_detection    : None ALWAYS (no column exists; review fix 2).
                                  reason in schema_findings.venue_route_unsupported_reason.
had_liquidity_fact_at_detection : True if paper_trade_entry_snapshots
                                  .liquidity_usd_at_entry IS NOT NULL; None if absent.
actionability_state_at_detection: from paper_trade_entry_snapshots.actionable_at_entry
                                  (1/0) else paper_trades.actionable; reason from
                                  *_reason_at_entry / paper_trades.actionability_reason;
                                  None if both absent.
# Each flag tri-state True / False / None(schema-absent). None NEVER -> False.
```

**Metric 5 — `appeared_on_gainers_timing_vs_peak` (RESCOPED; review fix 1)**
```
# Honest rename of "entered Today's Focus before/after move". There is NO
# persisted Today's-Focus membership table (get_todays_focus recomputes live;
# only localStorage persists). The ONLY persisted surface timestamp is
# gainers_comparisons.appeared_on_gainers_at (NOT NULL, db.py:938), gainers cohort only.
if metric5_data_path_available is False for this signal family:
    appeared_on_gainers_timing = "unsupported_for_signal"   # NOT "not surfaced", NOT 0
else:   # gainers cohort
    surf_ts = gainers_comparisons.appeared_on_gainers_at joined on coin_id=token_id
    if surf_ts is None: appeared_on_gainers_timing = "not_surfaced"   # real absence in a supported cohort
    elif peak_point is None: appeared_on_gainers_timing = "surfaced_no_observed_move"
    elif surf_ts <= peak_point.recorded_at: appeared_on_gainers_timing = "before_peak"
    else: appeared_on_gainers_timing = "after_peak"
# metric5_data_path_available is a per-signal bool in schema_findings:
#   True only for signal families whose rows join gainers_comparisons.
```

### 3.3 Intra-signal dedup (review fix 8)
Default `dedup=True`: within the cohort, collapse rows sharing `(token_id,
signal_type)` to the **earliest** `opened_at` for the usefulness metrics. The
collapsed multi-fire count is reported separately as `multi_fire_rows` per signal
(and `multi_fire_tokens`). **Do NOT dedup across different `signal_type`s** — a
token detected by two different signals is two independent scoring events.
`--no-dedup` disables collapsing; multi-fire counts are reported either way.

### 3.3a Per-horizon + aggregate immaturity gating (review fix 9)
Per row and horizon `h`: `mature_{h}h = (t0 + h <= now)`; `window_elapsed_fraction_{h}h`.
Immature observations are **excluded** from that horizon's aggregate and counted
in `immature_excluded_{h}h`. The **max-horizon** maturity additionally gates the
**time-to-peak** and **mae** aggregates (those are computed over the full observed
window, so a truncated window would bias them) — immature rows go to
`time_to_peak_immature_excluded` / `mae_immature_excluded`, not into the
distributions. (Distinct from "unjoinable": immature = window not elapsed;
unjoinable = no price points exist.)

### 3.4 Aggregation per `signal_type` (n-gated; review fixes 6,7,12)

```
group surviving rows by signal_type.
for each signal_type group:
    corpus           = derive_corpus(rows)             # micro-cap | cg-watcher | mixed
    n_total          = rows in cohort for this signal (post-dedup)
    multi_fire_rows  = rows collapsed by dedup
    n_joinable       = rows with >=1 post-detection price point
    n_unjoinable     = n_total - n_joinable
    metric5_data_path_available = any row joins gainers_comparisons
    if n_joinable < min_n:                              # BINARY floor (default 5)
        emit { "status":"INSUFFICIENT_DATA", "corpus":corpus,
               "n_total":n_total, "n_joinable":n_joinable, "n_unjoinable":n_unjoinable,
               "min_n":min_n, "metric5_data_path_available":metric5_data_path_available }
        continue                                       # NO metric values, NO false zeros
    metrics = {
       "time_to_peak_within_max_horizon_minutes":
            _float_distribution(mature values, min_samples=min_n_dist),  # None or dist
       "peak_at_window_edge_rate": rate_or_null(...),
       "mfe_1h":  {"n":k, "immature_excluded":m,
                   "dist": _float_distribution(mature 1h vals, min_samples=min_n_dist),
                   "low_confidence": k < min_n_dist},
       "mfe_4h":  {...},
       "mfe_24h": {...},
       "mae_before_favorable": {"n":k, "immature_excluded":m,
                   "dist": _float_distribution(..., min_samples=min_n_dist),
                   "low_confidence": k < min_n_dist},
       "favorable_reached_rate": k_fav / n_mature_joinable,    # rate or null
       "at_detection_facts": {                                 # fraction True over non-None
           "fresh_price_rate": rate_or_null(...),
           "venue_route_rate": null,                           # ALWAYS null (fix 2)
           "liquidity_fact_rate": rate_or_null(...),
           "actionable_rate": rate_or_null(...),
       },
       "appeared_on_gainers_timing": (                         # only if supported
           {"before_peak":x,"after_peak":y,"surfaced_no_move":z,"not_surfaced":w}
           if metric5_data_path_available else "unsupported_for_signal"),
    }
    emit { "status":"OK", "corpus":corpus, "n_total":n_total, "multi_fire_rows":multi_fire_rows,
           "n_joinable":n_joinable, "n_unjoinable":n_unjoinable,
           "metric5_data_path_available":metric5_data_path_available,
           "comparability_warning":"MFE/MAE comparable only within similar join-rate bands",
           "metrics":metrics }
```

**`_float_distribution(values, *, min_samples)` — FORKED, not imported (review
fix 12).** The reference `_points_distribution(points: list[int], *,
min_samples: int = 5)` is `int`-typed and hardcodes a floor of 5. This audit needs
**float** MFE/MAE values and a **configurable** floor (`min_n_dist`, default 10),
so it ships its own `_float_distribution(values: list[float], *,
min_samples: int)` returning `None` when `len(values) < min_samples`, else
`{min,p25,p50,p75,p90,max,mean}` over floats. `_quantile`, `_table_exists`,
`_column_exists`, `rate_or_null` are reused from the reference convention verbatim
(they swallow `sqlite3.Error` → `False`, so a missing table never crashes).

### 3.5 No live endpoint for metric 5
The reference fetches `/api/todays_focus` live. This audit does **not** (offline
contract, §4). Metric 5 is the persisted-proxy `appeared_on_gainers_timing`
(§3.2-M5), `unsupported_for_signal` for families without a persisted surface ts —
reported, never inferred, never zeroed.

### 3.6 `schema_findings` (PRAGMA-driven; corrected names)

```
schema_findings = {
  "paper_trades_has_signal_type":   _column_exists(conn,"paper_trades","signal_type"),
  "paper_trades_has_opened_at":     _column_exists(conn,"paper_trades","opened_at"),
  "paper_trades_has_token_id":      _column_exists(conn,"paper_trades","token_id"),
  "paper_trades_has_entry_price":   _column_exists(conn,"paper_trades","entry_price"),
  "volume_history_cg_has_price":    _column_exists(conn,"volume_history_cg","price"),
  "volume_history_cg_has_recorded_at": _column_exists(conn,"volume_history_cg","recorded_at"),
  "gainers_comparisons_present":    _table_exists(conn,"gainers_comparisons"),
  "gainers_comparisons_has_appeared_on_gainers_at":
        _column_exists(conn,"gainers_comparisons","appeared_on_gainers_at"),
  "gainers_comparisons_has_detected_price":
        _column_exists(conn,"gainers_comparisons","detected_price"),
  # CORRECT sidecar table name (was actionability_entry_snapshot in the blocked draft):
  "paper_trade_entry_snapshots_present":
        _table_exists(conn,"paper_trade_entry_snapshots"),
  "ptes_has_actionable_at_entry":
        _column_exists(conn,"paper_trade_entry_snapshots","actionable_at_entry"),
  "ptes_has_actionability_reason_at_entry":
        _column_exists(conn,"paper_trade_entry_snapshots","actionability_reason_at_entry"),
  "ptes_has_liquidity_usd_at_entry":
        _column_exists(conn,"paper_trade_entry_snapshots","liquidity_usd_at_entry"),
  "venue_route_unsupported_reason":
        "no venue column on paper_trades or paper_trade_entry_snapshots; "
        "venue_* tables are the BL-055 live layer keyed by (venue,symbol)",
  "alternate_price_history_tables_present": { name: _table_exists(...) for name in (
        "gainers_snapshots","losers_snapshots","momentum_7d",
        "slow_burn_candidates","volume_spikes") },
  "lookback_exceeds_retention": lookback_days > 7,
}
```

### 3.7 Joinable-vs-unjoinable as first-class output
Exactly as the reference treats coverage: every group reports `n_joinable` /
`n_unjoinable` so a low usefulness reading is attributable to "unjoinable key
space (contract address absent from volume_history_cg)" vs "joinable but no
favorable move." Combined with the `corpus` tag (§2.5) this is the §9c guard
against mis-attributing the outcome to the wrong lever.

---

## 4. OFFLINE-ONLY / read-only contract (testable invariants)

1. **Read-only DB:** `sqlite3.connect(f"file:{db}?mode=ro", uri=True)`. Test
   asserts `INSERT/UPDATE/DELETE/CREATE/DROP` raise `sqlite3.OperationalError`.
2. **No network:** module imports no `urllib`/`aiohttp`/`requests`; test asserts
   `urllib` not imported and `build_report` takes no URL argument.
3. **No live-ranking feed:** descriptive-only. No field ranks signals, emits an
   enable/disable verdict, or a TRADE/WATCH label. Test asserts top-level keys ⊆
   the allow-list (incl. `total_rows`) and none match
   `/rank|alert|enable|disable|verdict|prune|score_action/i`.
4. **No-lookahead:** immature horizon windows excluded from aggregates; counted in
   `immature_excluded_*` and gating time-to-peak/mae. Tested.
5. **No false zeros:** groups below the binary n-gate emit `INSUFFICIENT_DATA`;
   distributions below `min_n_dist` emit `LOW_CONFIDENCE`; unsupported metric-5
   families emit `unsupported_for_signal` — never zeroed. Tested.

---

## 5. Output JSON shape

```json
{
  "audited_at": "2026-05-29T22:00:00Z",
  "params": {
    "horizons_h": [1, 4, 24],
    "min_n": 5,
    "min_n_dist": 10,
    "fav_eps": 0.01,
    "lookback_days": 7,
    "dedup": true,
    "cohort_cutoff_iso": "2026-05-22T22:00:00+00:00",
    "now_iso": "2026-05-29T22:00:00+00:00"
  },
  "total_rows": 312,
  "signals": {
    "gainers_early": {
      "status": "OK",
      "corpus": "cg-watcher",
      "n_total": 121,
      "multi_fire_rows": 7,
      "n_joinable": 118,
      "n_unjoinable": 3,
      "metric5_data_path_available": true,
      "comparability_warning": "MFE/MAE comparable only within similar join-rate bands",
      "metrics": {
        "time_to_peak_within_max_horizon_minutes": {"min":5,"p25":22,"p50":61,"p75":140,"p90":410,"max":700,"mean":94.2},
        "peak_at_window_edge_rate": 0.12,
        "mfe_1h":  {"n":118,"immature_excluded":0,"low_confidence":false,"dist":{"min":0.0,"p25":0.01,"p50":0.04,"p75":0.11,"p90":0.3,"max":0.9,"mean":0.07}},
        "mfe_4h":  {"n":118,"immature_excluded":0,"low_confidence":false,"dist":{}},
        "mfe_24h": {"n":93,"immature_excluded":25,"low_confidence":false,"dist":{}},
        "mae_before_favorable": {"n":118,"immature_excluded":0,"low_confidence":false,"dist":{"min":-0.4,"p25":-0.12,"p50":-0.03,"p75":0.0,"p90":0.0,"max":0.0,"mean":-0.06}},
        "favorable_reached_rate": 0.83,
        "at_detection_facts": {
          "fresh_price_rate": 0.97,
          "venue_route_rate": null,
          "liquidity_fact_rate": 0.55,
          "actionable_rate": 0.74
        },
        "appeared_on_gainers_timing": {"before_peak":70,"after_peak":18,"surfaced_no_move":9,"not_surfaced":21}
      }
    },
    "chain_completed": {
      "status": "OK",
      "corpus": "micro-cap",
      "n_total": 14,
      "multi_fire_rows": 1,
      "n_joinable": 5,
      "n_unjoinable": 9,
      "metric5_data_path_available": false,
      "comparability_warning": "MFE/MAE comparable only within similar join-rate bands",
      "metrics": {
        "time_to_peak_within_max_horizon_minutes": null,
        "mfe_1h": {"n":5,"immature_excluded":0,"low_confidence":true,"dist":null},
        "at_detection_facts": {"fresh_price_rate":1.0,"venue_route_rate":null,"liquidity_fact_rate":0.6,"actionable_rate":0.8},
        "appeared_on_gainers_timing": "unsupported_for_signal"
      }
    },
    "slow_burn": {
      "status": "INSUFFICIENT_DATA",
      "corpus": "cg-watcher",
      "n_total": 3, "n_joinable": 2, "n_unjoinable": 1, "min_n": 5,
      "metric5_data_path_available": false
    }
  },
  "schema_findings": { "...": "see §3.6" }
}
```

Top-level keys (allow-list, review fix 12): `audited_at`, `params`, `total_rows`,
`signals`, `schema_findings`.

Human (default non-`--json`) output mirrors the reference `_format_human`: header
(`audited_at`, params), per-signal block (with `corpus`, `n_joinable/n_unjoinable`,
`LOW_CONFIDENCE`/`unsupported_for_signal` markers), then `SCHEMA FINDINGS:`.

---

## 6. Pinned decisions (formerly reviewer ambiguities)

All blocked-draft R-items are now PINNED, not deferred:
- **Entry basis (was R3):** `paper_trades.entry_price` for every signal (§2.1).
- **Repeat detections (was R1):** dedup to earliest per `(token,signal)`, default
  ON; multi-fire reported separately (§3.3).
- **`fav_eps` (was R5):** 0.01, configurable (§3.0).
- **Metric-5 source (was R6):** `gainers_comparisons.appeared_on_gainers_at`,
  gainers-only; `unsupported_for_signal` elsewhere (§3.2-M5).
- **Runner-board source (R4):** no persisted ts exists → local peak over
  `volume_history_cg` is the pinned definition (documented non-blocker, §3.2-M1).
- **`lookback_days > 7`:** allowed up to 90, flags `lookback_exceeds_retention`;
  joinable/unjoinable counts make the data loss visible (§3.6).

---

## 7. TDD test plan (`tests/test_audit_signal_early_usefulness.py`)

Mirror reference fixture style: module via `importlib.util.spec_from_file_location`
on `scripts/audit_signal_early_usefulness.py`; `tmp_path` sqlite;
`FIXED_NOW = datetime(2026, 5, 29, 22, 0, 0, tzinfo=utc)`; `_ro_conn()` helper;
`_insert_*` helpers building `paper_trades` (with `entry_price`) +
`volume_history_cg` (+ `gainers_comparisons` / `paper_trade_entry_snapshots`) rows
relative to `FIXED_NOW`.

1. **Per-metric correctness (P0 = entry_price)**
   - `test_entry_price_is_p0_for_all_signals` — moves computed from
     `paper_trades.entry_price`, not first volume point; assert a chain row and a
     gainers row both use entry_price as denominator.
   - `test_time_to_peak_picks_argmax_within_max_horizon` — peak at +90m ⇒ metric 90.
   - `test_peak_at_window_edge_flag` — peak on last point of an immature window ⇒
     `peak_at_window_edge` True.
   - `test_mfe_per_horizon_uses_only_in_window_points` — +30m(+5%), +3h(+12%),
     +20h(+40%) ⇒ mfe_1h=0.05, mfe_4h=0.12, mfe_24h=0.40.
   - `test_mfe_horizon_with_no_points_in_window_is_none` ⇒ mfe_1h None, not 0.
   - `test_mae_before_favorable_respects_fav_eps` — a +0.005 blip is NOT favorable
     (fav_eps 0.01); a later +2% is ⇒ mae taken over the pre-favorable window.
   - `test_mae_zero_when_favorable_on_first_point` ⇒ 0.0.
   - `test_mae_full_window_when_never_favorable` ⇒ worst drawdown,
     `favorable_reached` False.
2. **Metric 4 — at-detection fact flags + venue permanently None**
   - `test_fact_flags_true_when_snapshot_present` — `paper_trade_entry_snapshots`
     row with `liquidity_usd_at_entry`, `actionable_at_entry=1` ⇒ liquidity True,
     actionable True, fresh_price True.
   - `test_fact_flags_none_when_snapshot_table_absent` — drop
     `paper_trade_entry_snapshots` ⇒ flags None (surfaced in schema_findings),
     NOT False.
   - **`test_venue_route_flag_permanently_none`** (review fix 2) — even with a fully
     populated snapshot, `had_venue_route_at_detection` is `None` and
     `at_detection_facts.venue_route_rate` is `null`; assert never True/False.
   - `test_correct_sidecar_table_name_pragma` (review fix 3) — schema_findings key
     is `paper_trade_entry_snapshots_present` (NOT `actionability_entry_snapshot`)
     and reflects the real PRAGMA result.
   - `test_fresh_price_cohort_neutral_not_from_detected_price_for_non_gainers`
     (review fix 5) — a chain row with a snapshot but no gainers row ⇒ fresh_price
     True via snapshot; assert detected_price path is not consulted for it.
3. **Metric 5 — appeared-on-gainers timing + unsupported_for_signal**
   - `test_metric5_before_peak` / `test_metric5_after_peak` (gainers cohort,
     `appeared_on_gainers_at` vs peak).
   - `test_metric5_surfaced_no_move`.
   - **`test_metric5_unsupported_for_non_gainers_signal`** (review fix 1) — a
     chain/momentum signal with no `gainers_comparisons` join ⇒
     `appeared_on_gainers_timing == "unsupported_for_signal"` and
     `metric5_data_path_available == False`; assert it is NOT "not_surfaced" and
     NOT 0.
   - `test_metric5_not_surfaced_in_supported_cohort` — gainers family, token
     absent from `gainers_comparisons` ⇒ `"not_surfaced"` (distinct from
     unsupported).
4. **n-gate INSUFFICIENT_DATA + LOW_CONFIDENCE (review fix 7)**
   - `test_signal_below_binary_floor_emits_insufficient_data` (n_joinable=4,
     min_n=5) ⇒ INSUFFICIENT_DATA, no `metrics`, no zeroed values.
   - `test_signal_at_binary_floor_emits_metrics` (n_joinable=5).
   - **`test_distribution_below_min_n_dist_marks_low_confidence`** — n_joinable=7,
     min_n=5, min_n_dist=10 ⇒ status OK but each distribution `low_confidence:true`
     and `dist:null` (no false percentiles on n=7).
   - `test_distribution_at_min_n_dist_emits_dist` (n=10).
   - `test_custom_min_n_and_min_n_dist_via_args`.
5. **Immature / no-lookahead (per-horizon + aggregate gating, review fix 9)**
   - `test_immature_24h_window_excluded_from_mfe_aggregate` — detection at now-2h,
     horizon 24h ⇒ in `immature_excluded_24h`, present in mfe_1h.
   - **`test_immature_max_horizon_excludes_time_to_peak_and_mae`** — immature
     max-horizon row excluded from time-to-peak AND mae aggregates (counted in
     `time_to_peak_immature_excluded` / `mae_immature_excluded`), not mixed with
     mature rows.
   - `test_window_elapsed_fraction_reported`.
   - `test_detection_at_or_after_now_has_empty_path`.
6. **Intra-signal dedup (review fix 8)**
   - **`test_dedup_collapses_repeat_fires_to_earliest`** — same `(token_id,
     signal_type)` at +0h and +3h ⇒ one usefulness row at the earliest opened_at;
     `multi_fire_rows == 1`.
   - `test_dedup_does_not_collapse_across_signals` — same token, two different
     `signal_type`s ⇒ two independent rows.
   - `test_no_dedup_flag_keeps_all_rows` — `dedup=False` keeps both fires;
     multi-fire still reported.
7. **Corpus tag + join framing (review fix 6)**
   - **`test_corpus_tag_present_per_signal`** — chain-sourced ⇒ `micro-cap`,
     CG-watcher ⇒ `cg-watcher`.
   - `test_n_joinable_unjoinable_present_in_every_metric_block`.
   - `test_comparability_warning_present`.
8. **Joinable vs unjoinable**
   - `test_contract_address_token_id_unjoinable_reported` — 0x address absent from
     volume_history_cg ⇒ `n_unjoinable++`, not "no move."
   - `test_cg_slug_token_id_joins_directly`.
9. **Null / zero / negative price exclusion** — None/0/neg prices excluded; only
   positive-finite counts.
10. **Boundary inclusivity** — points exactly at `t0` and `t0 + h` counted;
    `t0 - 1s` and `t0 + h + 1s` excluded.
11. **`main()` exit paths**
    - `test_main_rejects_bad_horizons` (`--horizons "0,abc"`) ⇒ rc 2, stage args.
    - `test_main_rejects_horizon_above_168` ⇒ rc 2.
    - `test_main_rejects_min_n_below_1` / `test_main_rejects_min_n_dist_below_1` /
      `test_main_rejects_negative_fav_eps` ⇒ rc 2.
    - `test_main_db_open_failure_returns_2` (nonexistent db) ⇒ rc 2, stage db_open.
    - `test_main_smoke_empty_db_returns_0` ⇒ rc 0, `audited_at` ends `Z`,
      `params.min_n_dist == 10`, `params.fav_eps == 0.01`.
12. **Read-only enforcement**
    - `test_ro_connection_blocks_writes` — INSERT on ro conn raises OperationalError.
    - `test_module_imports_no_network` — `urllib` not imported; `build_report` has
      no `url` param.
13. **schema_findings PRAGMA runtime** — recreate `paper_trades` without
    `entry_price` ⇒ `paper_trades_has_entry_price` False, no crash; recreate
    `volume_history_cg` without `price` ⇒ `volume_history_cg_has_price` False.
14. **`_float_distribution` fork (review fix 12)**
    - `test_float_distribution_handles_floats` — float inputs (negative MAE values)
      produce float percentiles; assert it is NOT the int-typed reference helper.
    - `test_float_distribution_respects_configurable_floor` — `min_samples=10`
      returns None at n=9, a dict at n=10.
15. **Output contract**
    - **`test_top_level_keys_allowlist_includes_total_rows`** (review fix 12) —
      keys ⊆ {audited_at, params, total_rows, signals, schema_findings}; none match
      the forbidden ranking/alert regex.

---

## 8. Drift note (§7a)

Grep at design time confirms NO existing `scripts/audit_signal_*` early-usefulness
audit in tree; nearest neighbors are `scripts/audit_price_path_coverage.py`
(coverage, not usefulness), `scripts/audit_liquidity_coverage.py`, and the
`/api/signal_trust_scorecards` live dashboard surface. This standalone offline
audit is the narrowing step the backlog requested; it does not duplicate an
existing primitive. Every column it reads is verified present in `scout/db.py`
(line numbers cited throughout §0 and §2).
