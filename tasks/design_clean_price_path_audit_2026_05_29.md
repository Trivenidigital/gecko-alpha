**New primitives introduced:** [NONE]

This design adds a single offline, read-only diagnostic script
(`scripts/audit_clean_price_path.py`) plus its test module. It introduces no
new DB tables, no new writers, no new pipeline stages, no new config keys, no
new API endpoints, and no new runtime services. It mirrors the conventions of
the existing `scripts/audit_price_path_coverage.py` exactly. Therefore: NONE.

## Hermes-first analysis

Per global CLAUDE.md §7b, every design must document a Hermes skill-hub check
(`hermes-agent.nousresearch.com/docs/skills`) plus an awesome-hermes-agent
ecosystem check, even when the honest verdict is "build from scratch."

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Price-path / time-series shape classification (MFE/MAE, peak detection, flat-gap segmentation) | none found — Hermes skill hub exposes agent-orchestration / messaging / retrieval skills, not numeric intraday-series shape classifiers tied to a project's own sqlite | Build from scratch (offline, project-specific sqlite attribution; pure arithmetic over `volume_history_cg` rows — no external service adds value) |
| Trade attribution / runner labeling | none found — no skill maps detection events to post-detection price outcomes against a local trade ledger | Build from scratch (joins `paper_trades` / `gainers_comparisons` to `volume_history_cg` by project-specific identity keys; CG-slug-vs-contract caveat is gecko-alpha-specific) |
| Generic statistical summarization (rate-or-null) | none found that is worth a network hop | Build from scratch (the implemented script uses a small in-file `_rate_or_null` helper only; the reference's `_quantile` / `_points_distribution` helpers were evaluated and DROPPED as unnecessary for this audit — tiny, deterministic, offline) |

**awesome-hermes-agent ecosystem check + verdict:** Surveyed the
awesome-hermes-agent ecosystem categories (agent frameworks, tool/skill
registries, messaging/notification adapters, retrieval/RAG, on-chain
execution adapters). None provide an offline post-detection price-path
classifier over a local sqlite ledger; every listed capability is either
networked, execution-oriented, or orchestration-oriented. **Verdict: none
apply — this is offline project-specific sqlite attribution arithmetic; build
from scratch, mirroring the in-tree `audit_price_path_coverage.py` reference.**

---

# Design: BL-NEW-CLEAN-PRICE-PATH-AUDIT — offline runner-attribution price-path classifier

**Status of source spec:** PROPOSED 2026-05-28 — offline audit only
(`backlog.md` BL-NEW-CLEAN-PRICE-PATH-AUDIT, lines ~170-186; identical text in
`.spec_clean_price_path.txt`).
**Branch / worktree:** `feat/clean-price-path-audit` @
`C:\projects\gecko-alpha-wt\clean-price-path` (based on `origin/master`).
**Reference to mirror:** `scripts/audit_price_path_coverage.py` +
`tests/test_audit_price_path_coverage.py`.

> **OFFLINE-ONLY BANNER (enforced in docstring + every output payload):**
> This script intentionally consumes post-detection (future-relative-to-
> detection) price data BECAUSE it is offline hindsight attribution. It MUST
> NOT feed live ranking, curation, alerting, sizing, or signal enable/disable.
> Any such use requires a separate no-lookahead design (per backlog Rule:
> "V1 is read-only" and the spec guardrail "Keep output offline; no live
> ranking or curation changes without a follow-up design").

---

## SCOPE-LIMITATION (read this first — honest bound on what V1 can observe)

**`volume_history_cg` retains only ~7 days of price points** (the writer prunes
rows older than 7 days; this is the same retention fact that caps
`--window-hours` at 168). One consequence is load-bearing for honest
interpretation of the buckets and MUST NOT be glossed over:

**Genuine "runs weeks later" catalysts are UNOBSERVABLE in V1.** A candidate
that languishes flat for the entire 7-day post-detection window and only runs
*after* the window (days or weeks later) did NOT run *within* its observable
window. Within the data this audit can see, it is flat → it correctly lands in
`no_significant_move`. It does **NOT** land in `window_incomplete` or
`insufficient_data`:

- `window_incomplete` means the window has not yet *matured* (detection +
  maturity_hours > now) — a timing condition, not a "ran later" condition.
- `insufficient_data` means P0 is unresolvable or there are fewer than
  `min_points` price points in-window — a data-density condition.

Neither captures "flat for 7d, then ran in week 3." That recurrence is simply
invisible: by the time the late run happens, the early window's price points
have been pruned and/or the run falls outside the 168h window ceiling.

Therefore: **`unrelated_later_move` only captures the flat-then-run pattern that
occurs ENTIRELY WITHIN the 7-day window** — i.e. a long flat/stale span
(`>= flat_gap_hours`) followed by a run, both inside `[detection,
detection+window_hours]`. True weeks-later recurrence — the "runs weeks later"
phrasing in the spec — requires a longer-retention price source and is
explicitly **out of V1 scope** (see DD-1; the reference's documented "PR-C
decides whether to widen the data source" applies here too). V1 does not, and
cannot, claim to detect weeks-later catalysts; it claims only to classify the
post-detection path *within the observable 7-day window*.

---

## 0. Faithful capture of the operator's spec

Captured verbatim from `.spec_clean_price_path.txt` / `backlog.md`. The design
below MUST match these exactly; where the operator left a value unspecified, a
default is proposed and **FLAGGED (DD-n)** for reviewer judgment.

**Goal (verbatim):** "For candidates that later ran, classify the
post-detection price path into usable movement buckets: continuous move,
drawdown-then-recovery, or stale/unrelated later catalyst."

**Operator-named buckets (verbatim — names are pinned, do not rename):**
- `continuous_move`: "price path trends favorably after detection with shallow
  adverse excursion."
- `drawdown_then_recovery`: "candidate was early but required drawdown
  tolerance."
- `unrelated_later_move`: "later run occurs after a long flat/stale window; do
  not count as a live false negative."

**Required guardrails (verbatim):**
1. "Pin runner definition before the audit."
2. "Require temporal ordering: runner event after detection/surface
   timestamp."
3. "Require lookback maturity for all cohorts."
4. "Keep output offline; no live ranking or curation changes without a
   follow-up design."

**Anti-scope (from the spec + the enclosing backlog Rule, line 77 / line 168 /
line 212):** offline / read-only only; no live ranking, no curation changes,
no alerts, no sizing, no execution, no auto-enable/disable of signals, no
source pruning, no future-runner labels in live ranking.

**What the operator did NOT pin (→ design decisions, flagged DD-1..DD-9 below):**
the numeric thresholds for "shallow adverse excursion," the favorable-move
("ran") threshold, the "long flat/stale window" duration, the post-detection
window length, the maturity floor, and the bucket decision-tree ordering. All
are proposed with conservative, override-able CLI defaults and flagged.

---

## 1. CLI (argparse) — mirrors the reference's arg-validation + exit-code style

`main()` returns exit code **0** on success, **2** on argument-validation
failure, fetch/db-open failure, or any pre-`build_report` error (mirrors the
reference, which returns 2 for `stage="args" | "fetch" | "db_open"`). There is
no network fetch in this script (cohort comes from the DB itself, not an HTTP
endpoint — see §3), so the `fetch` stage of the reference is replaced by a
`cohort` stage (DB query that builds the cohort) which also exits 2 on error.

```
usage: audit_clean_price_path.py
       [--db PATH]
       [--cohort {paper,gainers,both}]
       [--window-hours INT]          # post-detection path window (P0 .. P0+W)
       [--run-threshold FLOAT]       # MFE pct to qualify as "ran"
       [--drawdown-threshold FLOAT]  # MAE-before-favorable pct splitting
                                     #   continuous_move vs drawdown_then_recovery
       [--flat-gap-hours FLOAT]      # max flat/stale gap before the run that
                                     #   reclassifies as unrelated_later_move
       [--flat-band-pct FLOAT]       # +/- band defining "flat" for gap detection
       [--min-points INT]            # min price points in window for a verdict
       [--maturity-hours FLOAT]      # detection_ts + maturity-hours must be <= now
       [--lookback-days INT]         # cohort selection: detections within N days
       [--sensitivity]               # add a `sensitivity` sweep block (default off)
       [--json]
```

| Arg | Default | Validation → exit 2 if violated | Source |
|---|---|---|---|
| `--db` | `scout.db` | n/a (db-open failure handled at `db_open` stage) | reference parity |
| `--cohort` | `both` | argparse `choices={paper,gainers,both}` | DD-9 |
| `--window-hours` | `168` (7d) | `1 <= W <= 168` — capped at `volume_history_cg` 7-day writer retention (`LOOKBACK_HOURS_CEILING=168` in the reference); a window longer than retention cannot be fully observed | **DD-1** + retention fact |
| `--run-threshold` | `30.0` (% MFE) | `> 0` | **DD-2** |
| `--drawdown-threshold` | `15.0` (% MAE-before-favorable) | `> 0` | **DD-3** |
| `--flat-gap-hours` | `48.0` | `> 0` | **DD-4** |
| `--flat-band-pct` | `10.0` (+/- %) | `> 0` **AND `flat_band_pct < run_threshold`** (a 'flat' band must be strictly narrower than a 'run', else a "flat" window could itself contain a run — same exit-2 discipline as the window-hours bound) | **DD-5** |
| `--min-points` | `5` | `>= 2` (need >=2 points to compute any excursion; <5 also nulls the distribution per reference convention) | **DD-6** |
| `--maturity-hours` | equals `--window-hours` | `> 0` | **DD-7** (guardrail #3) |
| `--lookback-days` | `30` | `>= 1` | **DD-8** |
| `--sensitivity` | off | n/a (additive `sensitivity` block; see §3.7) | fold (review) |
| `--json` | off | n/a | reference parity |

Validation failures emit, mirroring the reference exactly:
```json
{"status":"error","stage":"args","error":"<message>"}
```
to stdout when `--json`, else the message to stderr; return 2.

**Cross-argument validation (exit 2):** in addition to the per-arg bounds
above, `main()` rejects `flat_band_pct >= run_threshold` with
`{"stage":"args"}` / exit 2. Rationale: the "flat" band defines the window
treated as stale (no meaningful move); if it were as wide as or wider than the
"ran" threshold, a span the classifier counts as "flat" could simultaneously
contain a qualifying run, making `unrelated_later_move` incoherent. The 'flat'
band MUST be strictly narrower than a 'run'. This is the same exit-2 discipline
applied to the `--window-hours` ceiling. (Tested by
`test_main_rejects_flat_band_ge_run_threshold`.)

`now = datetime.now(timezone.utc)` is established once in `main()` and injected
into `build_report` (never read inside the pure core) — reference parity.

---

## 2. Pure core: `build_report(...)` signature + classification algorithm

### 2.1 Signature (pure, injected `now`, no I/O except the read-only `conn`)

```python
def build_report(
    cohort_rows: list[dict],   # one dict per detected candidate (see §3.4)
    conn: sqlite3.Connection,  # opened file:...?mode=ro by main()
    *,
    window_hours: int,
    run_threshold: float,
    drawdown_threshold: float,
    flat_gap_hours: float,
    flat_band_pct: float,
    min_points: int,
    maturity_hours: float,
    now: datetime,
) -> dict[str, Any]:
```

Like the reference, `build_report` takes already-fetched rows + a read-only
`conn`; it pulls each token's price series via `conn` but writes nothing.

### 2.2 Establishing P0 (detected price) and the price series — pseudocode

```
for each candidate in cohort_rows:
    coin_id        = candidate["coin_id"]        # join key (see §3 caveat)
    detection_ts   = candidate["detection_ts"]   # ISO-8601 UTC string
    detection_dt   = parse_iso_utc(detection_ts)

    # ---- maturity guard (guardrail #3 + spec's window_incomplete bucket) ----
    if detection_dt + maturity_hours > now:
        bucket = "window_incomplete"          # MUST NOT be misclassified
        emit_row(coin_id, bucket, mfe=None, mae=None, time_to_peak_h=None)
        continue

    # ---- establish P0 ----
    # Prefer the ledger's recorded detected price when present & valid;
    # else fall back to the first valid in-window price point.
    P0 = candidate.get("detected_price")
    if not is_valid_price(P0):                # None / <=0 / >=INFINITY_GUARD_MAX
        P0 = first_valid_price_in_window(conn, coin_id, detection_dt, window_hours)
    if P0 is None:
        bucket = "insufficient_data"
        emit_row(coin_id, bucket, None, None, None)
        continue

    # ---- pull the post-detection series, AT-OR-AFTER detection ----
    # Lower bound is INCLUSIVE of the detection instant: recorded_at >= detection_ts.
    series = price_points(
        conn, coin_id,
        start = detection_dt,                 # inclusive of detection instant (>=)
        end   = detection_dt + window_hours,  # inclusive upper bound (<=)
    )
    # is_valid_price filter applied in SQL: price IS NOT NULL AND price>0
    #   AND price < INFINITY_GUARD_MAX  (reference parity)
    # series sorted ascending by recorded_at.

    if len(series) < min_points:
        bucket = "insufficient_data"
        emit_row(coin_id, bucket, None, None, None)
        continue
```

### 2.3 Computing MFE, MAE-before-favorable, peak time, flat-gap — pseudocode

All excursions are percentages relative to P0. Time is measured from
`detection_dt`.

```
    # max favorable excursion over the window
    peak_price   = max(p.price for p in series)
    peak_dt      = recorded_at of the FIRST series point achieving peak_price
    MFE_pct      = (peak_price - P0) / P0 * 100
    time_to_peak_h = (peak_dt - detection_dt).total_seconds() / 3600

    # max ADVERSE excursion BEFORE the favorable peak (guardrail for the
    # "early but required drawdown tolerance" semantics)
    pre_peak     = [p for p in series if p.recorded_at <= peak_dt]
    trough_price = min(p.price for p in pre_peak)     # lowest dip up to the peak
    MAE_before_favorable_pct = (P0 - trough_price) / P0 * 100   # >=0 when below P0

    # flat/stale-gap detection: the longest contiguous run, BEFORE peak_dt,
    # during which every point stays within +/- flat_band_pct of P0.
    # "long flat window then a run" => unrelated_later_move.
    flat_gap_h = longest_flat_run_hours(
        points = [p for p in series if p.recorded_at <= peak_dt],
        P0 = P0, band_pct = flat_band_pct,
    )
```

`longest_flat_run_hours`: walk the pre-peak points in time order; a point is
"flat" if `abs((p.price - P0)/P0*100) <= flat_band_pct`; track the longest
contiguous flat span as `(last_flat_ts - first_flat_ts)` in hours; reset the
span on any non-flat point. Returns 0.0 if no flat span of >=2 points exists.

### 2.4 Decision tree → bucket (ORDER IS LOAD-BEARING; flagged DD-relevant)

```
# residual / non-attributable first (cannot be a runner):
if bucket already set to window_incomplete:    -> window_incomplete
if P0 missing or len(series) < min_points:     -> insufficient_data

# did it run at all?
if MFE_pct < run_threshold:                    -> no_significant_move

# it ran. Was the run preceded by a long flat/stale window?
#   (spec: "later run occurs after a long flat/stale window")
elif flat_gap_h >= flat_gap_hours:             -> unrelated_later_move

# it ran promptly. Shallow adverse excursion => clean continuous move.
#   (spec: "trends favorably ... with shallow adverse excursion")
elif MAE_before_favorable_pct <= drawdown_threshold:
                                               -> continuous_move

# it ran promptly but required tolerating a deeper dip first.
#   (spec: "early but required drawdown tolerance")
else:                                          -> drawdown_then_recovery
```

**Ordering rationale (flagged DD — reviewer must confirm):** `unrelated_later_move`
is tested BEFORE the drawdown/continuous split because the spec explicitly says
a long-flat-then-run is "a different opportunity" and must "not count as a live
false negative" — i.e. the flat-gap classification dominates the
excursion-shape classification. A token that languished for `flat_gap_hours`
then ran is `unrelated_later_move` regardless of its pre-peak dip depth.

### 2.5 Buckets (complete, closed set)

| Bucket | Meaning | Origin |
|---|---|---|
| `continuous_move` | ran (MFE >= run_threshold), prompt (flat_gap < flat_gap_hours), shallow dip (MAE_before_favorable <= drawdown_threshold) | operator |
| `drawdown_then_recovery` | ran, prompt, but required tolerating a dip > drawdown_threshold before the peak | operator |
| `unrelated_later_move` | ran, but only after a flat/stale window >= flat_gap_hours | operator |
| `no_significant_move` | matured + enough data, but MFE < run_threshold (never ran) | residual (required) |
| `insufficient_data` | matured but P0 unknown OR fewer than min_points in-window | residual (required) |
| `window_incomplete` | detection_ts + maturity_hours > now: window not matured, do NOT classify | residual (required, guardrail #3) |

**ONLY `window_incomplete` and `insufficient_data` rows null the metric
fields** (`mfe=null`, `mae=null`, `time_to_peak=null`). Every other bucket —
including `unrelated_later_move` and `no_significant_move` — carries
**non-null** `mfe` / `mae` / `time_to_peak`, because for those rows P0 was
resolved and a valid in-window series was computed, so the excursions are
real numbers. In particular, an `unrelated_later_move` row DID run (its MFE
reached `run_threshold`) and DID have a measurable pre-peak dip; nulling its
metrics would discard exactly the evidence that distinguishes it from the
prompt-run buckets. The metric-nulling rule is therefore: null iff the bucket
is `insufficient_data` or `window_incomplete`; otherwise emit the computed
values. (Tested by `test_unrelated_later_move_retains_metrics` in §5.)

The two metric-nulling buckets are excluded from the matured-rate denominator
where the operator wants "rate among classifiable runners" (see §3.3 — both a
gross and a matured-denominator rate are emitted so the choice is explicit and
auditable, never silently picked). **When that matured denominator is < 5, the
entire `bucket_rates_matured` block is nulled** (fold #8), reusing the
reference's N<5 convention at the rate-denominator level; `matured_denominator`
and `bucket_rates_matured_suppressed_reason` are emitted so the suppression is
visible rather than silent.

---

## 3. Output JSON shape (mirrors the reference)

### 3.1 Top level
```json
{
  "audited_at": "2026-05-29T14:00:00Z",        // _utc_iso_z(now), reference parity
  "offline_only_banner": "OFFLINE-ONLY hindsight attribution. MUST NOT feed live ranking/curation/alerting/sizing. See BL-NEW-CLEAN-PRICE-PATH-AUDIT.",
  "params": {                                   // every threshold echoed
    "cohort": "both",
    "window_hours": 168,
    "run_threshold": 30.0,
    "drawdown_threshold": 15.0,
    "flat_gap_hours": 48.0,
    "flat_band_pct": 10.0,
    "min_points": 5,
    "maturity_hours": 168.0,
    "lookback_days": 30
  },
  "total_cohort": 142,
  "bucket_counts": {
    "continuous_move": 18,
    "drawdown_then_recovery": 11,
    "unrelated_later_move": 7,
    "no_significant_move": 60,
    "insufficient_data": 30,
    "window_incomplete": 16
  },
  "bucket_rates_gross": {        // num / total_cohort, null when total_cohort==0
    "continuous_move": 0.1268, ...
  },
  "bucket_rates_matured": {      // num / (total_cohort - window_incomplete - insufficient_data)
    "continuous_move": 0.1875, ...   // see N<5 rule below
  },
  // NOTE (fold #8): when the matured denominator < 5, the ENTIRE
  // bucket_rates_matured block is null (not per-rate null) — reusing the
  // reference's N<5 convention at the RATE-denominator level, not just the
  // distribution level. A handful of matured rows cannot support a
  // trustworthy per-bucket rate, so the whole block is suppressed:
  //   "bucket_rates_matured": null,
  // and a companion field makes the suppression explicit:
  //   "matured_denominator": 3,
  //   "bucket_rates_matured_suppressed_reason": "matured_denominator < 5"
  "per_row": [
    {
      "coin_id": "some-token",
      "cohort_source": "paper",            // paper | gainers
      "bucket": "drawdown_then_recovery",
      "mfe": 41.2,                         // null for insufficient/incomplete
      "mae": 22.7,                         // MAE-before-favorable; null as above
      "time_to_peak": 53.5,                // hours; null as above
      "p0_basis": "ledger_detected_price"  // §3.4 P0 priority: which basis used
    }
  ],
  "join_failure_breakdown": { ... },       // §3.5 — insufficient_data split by source
  "gainers_runner_def_crosscheck": { ... },// §3.6 — computed MFE vs stored peak_gain_pct
  "schema_findings": { ... }               // §3.2
}
```

Rates use the reference's `_rate_or_null(num, denom)` (round 4, null when
`denom <= 0`). The dual gross/matured denominators make the "do not count
immature/insufficient as failures" guardrail explicit and auditable rather
than buried in a single ambiguous rate. Additionally, the matured-rate block is
suppressed wholesale (set to `null`) when its denominator < 5 (fold #8 — N<5
convention at the rate-denominator level), with `matured_denominator` and
`bucket_rates_matured_suppressed_reason` emitted alongside.

### 3.2 `schema_findings` — PRAGMA-driven, runtime (reference parity)
```json
"schema_findings": {
  "volume_history_cg_has_price": true,
  "volume_history_cg_has_recorded_at": true,
  "volume_history_cg_has_coin_id": true,
  "paper_trades_present": true,
  "paper_trades_detection_ts_column": "opened_at",   // resolved name or null
  "paper_trades_detected_price_column": "entry_price",
  "paper_trades_coin_id_column": "token_id",
  "gainers_comparisons_present": true,
  "gainers_comparisons_detected_price_column": "detected_price",
  "gainers_comparisons_detection_ts_column": "appeared_on_gainers_at",
  "gainers_comparisons_coin_id_column": "coin_id"
}
```
Column-name resolution is done at runtime via `PRAGMA table_info(<table>)`
(reuse `_column_exists`) against a candidate-name list, so the script reports
exactly which physical column it bound and degrades to `null` (and an
`insufficient_data` / empty-cohort result) instead of crashing if the schema
differs. **This is the enforcement mechanism for the §3.4 column-name caveat
below — the audit self-documents the columns it actually used.**

### 3.3 Human format (`_format_human`) mirrors the reference layout
audited_at / banner / echoed params / total_cohort / per-bucket count+gross+
matured rate block (with the N<5 suppression line when applicable) /
join_failure_breakdown block / gainers_runner_def_crosscheck block / per_row
lines / `sensitivity` block (only when `--sensitivity`) / schema findings
block.

### 3.4 Data sources, columns, join keys, and the CG-slug caveat

**`volume_history_cg` (CONFIRMED — reference script SQL + test DDL):**
columns `coin_id TEXT`, `price REAL`, `recorded_at TEXT` (ISO-8601 UTC string;
compared lexicographically via `>=` against an `.isoformat()` cutoff — the
reference relies on ISO sort==time sort). Writer:
`scout/spikes/detector.py` writes `(coin_id, price, recorded_at)`; rows older
than 7 days are pruned (hence the 168h window ceiling). This is the price
series source for P0-fallback and all excursions.

**`paper_trades` (CONFIRMED — `scout/db.py:996-1042`):** provides one row per
paper-trade-opened candidate. Confirmed columns:
- identity `token_id TEXT NOT NULL` (line 998) — the cohort join key.
- open/detection timestamp `opened_at TEXT NOT NULL` (line 1033) → `detection_ts`.
- detected/entry price `entry_price REAL NOT NULL` (line 1005) → P0 source.
- `signal_type TEXT NOT NULL` (line 1002).
- `UNIQUE(token_id, signal_type, opened_at)` (line 1042).
The schema comment at `db.py:992` confirms: "paper_trades.token_id references
candidates.contract_address or price_cache.coin_id logically" — i.e. for
CG-sourced rows `token_id` holds the CG slug, exactly the CG-slug caveat below.

**`gainers_comparisons` (CONFIRMED — `scout/db.py:932-954`):** provides
candidates with a detected price and the time they appeared on gainers.
Confirmed columns:
- `coin_id TEXT NOT NULL` (line 934) — the cohort join key (CG coin_id).
- `appeared_on_gainers_at TEXT NOT NULL` (line 938) → `detection_ts`.
- `detected_price REAL` (line 948, added via migration at `db.py:1255-1256`;
  nullable, so the P0-fallback path in §2.2 is load-bearing for this cohort).

**Join key + CG-slug caveat (CONFIRMED pattern from the reference):** the
reference joins by token id directly — `volume_history_cg.coin_id` is matched
against the cohort row's token id. Critically, for `chain=coingecko` rows the
`contract_address` field actually stores the **CoinGecko slug, not the on-chain
address** (documented project caveat; MEMORY.md
`feedback_cg_slug_not_address_for_cg_sourced_rows`). Because
`volume_history_cg` is keyed by CG `coin_id`/slug, the audit MUST join on the
CG slug/coin_id form, NOT the contract address. The reference's
`test_paper_row_with_cg_slug_token_id_joins_to_volume_history` confirms a paper
row whose `token_id` IS a CG slug joins directly; rows whose identity is a raw
contract address will not join and correctly fall to `insufficient_data` (zero
in-window points) rather than being silently mis-joined. No cross-source
`/coins/{id}.platforms` hop is performed in V1 (offline, no network); the
slug-vs-address mismatch is surfaced as `insufficient_data` and counted, never
hidden.

> **VERIFICATION NOTE (RESOLVED during this design pass):** all three tables'
> physical columns are CONFIRMED from `scout/db.py` CREATE TABLE statements
> (`volume_history_cg`:870, `paper_trades`:996, `gainers_comparisons`:932) and
> are quoted with file:line above. The runtime PRAGMA name-resolution in §3.2
> is retained as defense-in-depth (it makes a future schema drift fail loud
> with null + empty cohort rather than silently mis-attributing), but it is no
> longer a blocking unknown. Implementer should still run `PRAGMA
> table_info(...)` against the live `scout.db` to confirm migrated columns
> (`gainers_comparisons.detected_price`) are present on the target DB instance.

### 3.5 `join_failure_breakdown` — `insufficient_data` split by source/chain

A single aggregate `insufficient_data` count hides the failure mode where one
source/chain class systematically does not join `volume_history_cg` (e.g. raw
contract-address identities that are not CG slugs — see the CG-slug caveat in
§3.4). To make an enriched `insufficient_data` bucket for one class **visible
rather than silent**, the report emits a breakdown of the `insufficient_data`
rows split by cohort source and (where derivable) chain class:

```json
"join_failure_breakdown": {
  "insufficient_data_total": 30,
  "by_cohort_source": {                  // paper | gainers
    "paper": 22,
    "gainers": 8
  },
  "by_identity_class": {                 // coarse, derivable offline w/o network
    "cg_slug_like": 4,                   // identity matches a CG-slug shape
    "contract_address_like": 24,         // 0x… / base58 → never joins (caveat)
    "other": 2
  },
  "below_min_points_with_ledger_p0": 26, // P0 resolved but 1..min_points-1 valid pts (NOT strictly zero)
  "p0_unresolvable": 4                   // P0 itself could not be established
}
```

`by_identity_class` uses a cheap offline heuristic on the identity string only
(no network, no `/coins/{id}` hop): a `0x`-prefixed 40-hex string or a base58
length-typical string is classified `contract_address_like`; otherwise
`cg_slug_like`; ambiguous → `other`. This is a *diagnostic hint*, not a join
attempt — its purpose is to surface "the insufficient_data bucket is dominated
by contract-address identities that structurally cannot join" so the operator
does not misread the bucket as "missing intraday data." The split mirrors the
reference's joinable-vs-unjoinable first-class reporting at a finer grain.
(Tested by `test_join_failure_breakdown_splits_by_source_and_identity_class`.)

### 3.6 `gainers_runner_def_crosscheck` — computed MFE vs stored `peak_gain_pct`

`gainers_comparisons` already stores its own hindsight runner metrics —
`peak_price` and `peak_gain_pct` (CONFIRMED in `scout/db.py`: the
`gainers_comparisons` DDL declares `peak_price REAL` and `peak_gain_pct REAL`
alongside `coin_id` / `appeared_on_gainers_at` / `detected_price`). That is a
SECOND, pre-existing "runner" definition computed by a different code path than
this audit's MFE. Silently picking one definition would hide the discrepancy
and pre-empt the operator's ratification of the canonical runner def
(guardrail #1, "Pin runner definition before the audit").

So for the **gainers cohort only**, the audit emits BOTH numbers per row and a
count of disagreements:

```json
"gainers_runner_def_crosscheck": {
  "rows_compared": 40,                   // gainers rows with non-null stored peak_gain_pct
  "audit_ran_count": 18,                 // audit MFE >= run_threshold
  "stored_ran_count": 21,                // stored peak_gain_pct >= run_threshold
  "agree_count": 16,                     // both agree ran / both agree not-ran
  "disagree_audit_no_stored_yes": 4,     // audit says no-move, stored peak >= run_threshold
  "disagree_audit_yes_stored_no": 1,     // audit says ran, stored peak < run_threshold
  "per_row": [
    {"coin_id": "...", "audit_mfe": 12.3, "stored_peak_gain_pct": 48.0,
     "audit_ran": false, "stored_ran": true}
  ]
}
```

Both "directions" of disagreement are counted (audit-no/stored-yes AND
audit-yes/stored-no), making the "two runner definitions" issue fully
auditable. The crosscheck is **read-only and diagnostic** — it changes no
bucket assignment; the audit's own MFE remains the classifier input. It exists
solely so the operator can ratify which runner definition is canonical with the
disagreement magnitude in hand. (Tested by
`test_gainers_crosscheck_counts_both_disagreement_directions`.)

### 3.7 `sensitivity` block (optional, `--sensitivity`) — classification fragility

Bucket assignments depend on `run_threshold` and `drawdown_threshold`. A
classification that flips wildly under a small threshold nudge is fragile and
the operator should see that fragility rather than trust a single brittle
point estimate. When `--sensitivity` is passed, the report adds a `sensitivity`
block that recomputes per-bucket counts under a small sweep:

- `run_threshold ∈ {20, 30, 40}`
- `drawdown_threshold ∈ {10, 15, 20}`

i.e. the 3×3 = 9 combinations. For each cell it records the per-bucket counts;
it also summarises per-bucket count stability (min/max across the sweep) so a
bucket whose count swings dramatically is obvious:

```json
"sensitivity": {
  "run_threshold_sweep": [20, 30, 40],
  "drawdown_threshold_sweep": [10, 15, 20],
  "grid": [
    {"run_threshold": 20, "drawdown_threshold": 10,
     "bucket_counts": {"continuous_move": 24, "drawdown_then_recovery": 9, ...}},
    ...
  ],
  "per_bucket_count_range": {        // min..max of each bucket count across the 9 cells
    "continuous_move": {"min": 12, "max": 24},
    "drawdown_then_recovery": {"min": 6, "max": 19},
    "unrelated_later_move": {"min": 5, "max": 8},
    "no_significant_move": {"min": 48, "max": 71}
  }
}
```

The sweep reuses the SAME pure `build_report` machinery (it re-runs the
classifier with swept thresholds over the already-fetched cohort/series); it
introduces no new I/O. **Default OFF** to keep base output clean and cheap; the
flag opts in. The other swept-but-not-in-the-base-set thresholds
(`flat_gap_hours`, `flat_band_pct`, `min_points`, `maturity_hours`) are held at
their CLI values for the sweep — the fold scopes the sweep to the two movement
thresholds that drive the runner/dip split. (Tested by
`test_sensitivity_block_present_only_with_flag` and
`test_sensitivity_grid_has_nine_cells`.)

---

## 4. Lookahead / anti-scope contract (enforceable)

This script is offline hindsight attribution and intentionally reads
post-detection data. The following constraints are the enforceable contract;
each maps to a test in §5:

1. **Writes nothing.** No `INSERT/UPDATE/DELETE/CREATE`. Enforced by opening
   the DB read-only (`file:{db}?mode=ro`, `uri=True`) AND a test asserting a
   write attempt through the audit's connection raises `sqlite3.Operational
   Error` (read-only db). (T-RO)
2. **Read-only DB open** — reference parity, `stage="db_open"` → exit 2 on
   failure.
3. **Explicit OFFLINE-ONLY banner** in (a) the module docstring and (b) the
   `offline_only_banner` field of every JSON payload and the human output
   header. (T-BANNER)
4. **Excludes immature windows** — `window_incomplete` bucket; immature rows
   are never assigned a movement bucket and never enter the matured-rate
   denominator. (T-IMMATURE)
5. **Temporal ordering (guardrail #2)** — only price points
   **at-or-after detection** (`recorded_at >= detection_ts`, the lower bound
   INCLUSIVE of the detection instant) enter the series; the run/peak occurs
   at-or-after detection by construction. The wording is pinned as
   "at-or-after detection", never "strictly after", so the prose, the
   pseudocode, and the SQL all agree on the inclusive `>=` bound.
6. **No live coupling** — the script imports nothing from `scout.main`,
   `scout.gate`, `scout.scorer`, `scout.alerter`; it does not write any table,
   call any HTTP endpoint, or emit any alert. (Static: the file's only
   stdlib imports are argparse/json/sqlite3/sys/datetime/typing — reference
   parity. A test asserts no `scout.` business-logic import.) (T-NOIMPORT)

---

## 5. TDD test plan (mirrors `test_audit_price_path_coverage.py` fixture style)

Use `importlib.util.spec_from_file_location("audit_clean_price_path", ...)` to
load the script (reference parity), a `tmp_path` sqlite fixture creating
`volume_history_cg` (+ minimal `paper_trades` / `gainers_comparisons` with the
resolved columns), a module-scoped `FIXED_NOW`, an `_ro_conn(db_path)` helper,
and `_insert_point(conn, coin_id, price, hours_after_detection)`.

Pin `FIXED_NOW = datetime(2026, 5, 29, 14, 0, 0, tzinfo=timezone.utc)`.

**One test per bucket, incl. boundaries:**
- `test_continuous_move` — MFE >= run_threshold, flat_gap < flat_gap_hours, MAE_before_favorable <= drawdown_threshold.
- `test_drawdown_then_recovery` — same but a pre-peak dip just OVER drawdown_threshold.
- `test_drawdown_boundary_at_exactly_threshold_is_continuous` — MAE == drawdown_threshold ⇒ `continuous_move` (`<=` inclusive; pin the boundary).
- `test_unrelated_later_move` — long flat run (>= flat_gap_hours) then a spike; assert it wins over the dip-based split.
- `test_flat_gap_boundary_exactly_at_threshold_is_unrelated` — flat_gap == flat_gap_hours ⇒ `unrelated_later_move` (`>=` inclusive).
- `test_run_threshold_boundary_below_is_no_significant_move` — MFE just under run_threshold ⇒ `no_significant_move`; MFE == run_threshold ⇒ a run (pin `>=` semantics: not-ran uses `MFE < run_threshold`).
- `test_no_significant_move` — matured, >=min_points, MFE well below threshold.

**Residual / guard buckets:**
- `test_window_incomplete_when_not_matured` — detection_ts + maturity_hours > FIXED_NOW ⇒ `window_incomplete`, mfe/mae/time_to_peak all null, excluded from matured denominator.
- `test_insufficient_data_when_below_min_points` — matured but <min_points in-window ⇒ `insufficient_data`.
- `test_insufficient_data_when_p0_unresolvable` — no detected_price and no valid first in-window point ⇒ `insufficient_data`.

**Metric retention (fold #3):**
- `test_unrelated_later_move_retains_metrics` — an `unrelated_later_move` row carries non-null `mfe` / `mae` / `time_to_peak` (only `insufficient_data` and `window_incomplete` null them).

**Price-validity + join:**
- `test_null_zero_negative_prices_excluded` — null/0/negative/+Inf-guard prices excluded from series (reference parity: `price IS NOT NULL AND price>0 AND price<INFINITY_GUARD_MAX`).
- `test_pre_detection_points_excluded` — points with `recorded_at < detection_ts` never enter the series (guardrail #2; lower bound is INCLUSIVE so a point AT detection_ts counts, a point one second before does not).
- `test_lower_bound_inclusive_at_detection_instant` — a price point at exactly `recorded_at == detection_ts` IS included (pins the inclusive `>=` lower bound, fold #2).
- `test_cg_slug_token_joins_contract_address_does_not` — a CG-slug identity joins; a raw contract-address identity yields zero points ⇒ `insufficient_data`. This is a NEW test establishing the contract-address-does-not-join behavior for THIS script (the reference's join test is for a different cohort shape and is not cited as proof here).

**Join-failure breakdown (fold #4):**
- `test_join_failure_breakdown_splits_by_source_and_identity_class` — `insufficient_data` rows are split by cohort source (paper/gainers) AND by identity class (`cg_slug_like` / `contract_address_like` / `other`), so an enriched bucket for one class is visible; assert a contract-address-dominated cohort shows it in `by_identity_class`.

**Gainers runner-def crosscheck (fold #5):**
- `test_gainers_crosscheck_counts_both_disagreement_directions` — for the gainers cohort, both `audit_mfe` and stored `peak_gain_pct` are emitted per row; assert `disagree_audit_no_stored_yes` and `disagree_audit_yes_stored_no` are each counted on a fixture containing one of each.

**Sensitivity sweep (fold #7):**
- `test_sensitivity_block_absent_without_flag` — no `sensitivity` key in the report when `--sensitivity` not passed (default off).
- `test_sensitivity_block_present_only_with_flag` / `test_sensitivity_grid_has_nine_cells` — with `--sensitivity`, a `sensitivity` block exists with a 3×3=9-cell grid and a `per_bucket_count_range` summary.

**Distribution / rate nulling:**
- `test_bucket_rates_null_when_denominator_zero` — empty cohort ⇒ gross + matured rates null (reference `_rate_or_null`).
- `test_matured_rate_excludes_incomplete_and_insufficient` — denominator math.
- `test_matured_rate_block_nulled_below_5` (fold #8) — when the matured denominator < 5, the WHOLE `bucket_rates_matured` block is `null`, with `matured_denominator` and `bucket_rates_matured_suppressed_reason` emitted.
- `test_matured_rate_block_present_at_or_above_5` (fold #8) — at matured denom >= 5 the block is populated.
- `test_points_distribution_null_below_5` / `test_distribution_emitted_at_or_above_5` — if a points-distribution summary is emitted, mirror the reference N<5 null rule. (DD: distribution over per-token in-window point counts; FLAGGED whether to include — see §6.)

**`main()` exit-code discipline:**
- `test_main_rejects_window_above_ceiling` — `--window-hours 200` ⇒ exit 2, `{"stage":"args"}`.
- `test_main_rejects_nonpositive_run_threshold` — `--run-threshold 0` ⇒ exit 2, args.
- `test_main_rejects_flat_band_ge_run_threshold` (fold #6) — `--flat-band-pct 30 --run-threshold 30` ⇒ exit 2, `{"stage":"args"}` (flat band must be strictly narrower than run threshold).
- `test_main_rejects_bad_cohort` — argparse `choices` rejects unknown cohort (SystemExit / exit 2).
- `test_main_smoke_empty_cohort_returns_0` — monkeypatch cohort builder to `[]` ⇒ exit 0, `total_cohort==0`, `audited_at` ends `Z`, params echoed.
- `test_main_db_open_failure_returns_2` — nonexistent db path ⇒ exit 2, `{"stage":"db_open"}`.
- `test_main_cohort_query_failure_returns_2` — cohort-build SQL error (e.g., table missing) ⇒ exit 2, `{"stage":"cohort"}` (the no-network analogue of the reference's `fetch` stage).

**Contract enforcement:**
- `test_read_only_db_blocks_writes` (T-RO) — a write via the audit's read-only conn raises `sqlite3.OperationalError`.
- `test_offline_banner_present_in_json_and_human` (T-BANNER).
- `test_no_business_logic_imports` (T-NOIMPORT) — assert the module does not import `scout.main/gate/scorer/alerter` (inspect `module.__dict__` / source).

---

## 6. Edge cases & decisions FLAGGED for reviewer judgment

- **DD-0 (RESOLVED — not blocking):** `paper_trades` / `gainers_comparisons` /
  `volume_history_cg` columns are CONFIRMED from `scout/db.py` (file:line in
  §3.4). `paper_trades`: `token_id` / `opened_at` / `entry_price` /
  `signal_type`. `gainers_comparisons`: `coin_id` / `appeared_on_gainers_at` /
  `detected_price` (nullable, migration-added). Implementer should still
  PRAGMA-verify the live DB instance for the migrated `detected_price` column.
- **DD-1 window_hours default = 168h (7d).** Bounded by `volume_history_cg`
  7-day retention. Operator did not pin a window. Longer "weeks later"
  catalysts (the spec mentions "runs weeks later") CANNOT be observed within
  retention — such tokens correctly land in `window_incomplete` /
  `insufficient_data` rather than being mislabeled. Reviewer: confirm 7d is
  the intended attribution horizon, or whether a wider price source is needed
  (out of V1 scope per the reference's documented "PR-C decides whether to
  widen the data source").
- **DD-2 run_threshold default = 30% MFE** as the "ran" definition (guardrail
  #1 "Pin runner definition before the audit"). 30% mirrors the project's
  moonshot/peak vocabulary but is a proposal — the operator must ratify the
  pinned runner threshold.
- **DD-3 drawdown_threshold default = 15% MAE-before-favorable** splits
  `continuous_move` (shallow) vs `drawdown_then_recovery` (deep). The spec only
  says "shallow"; 15% is a proposed boundary.
- **DD-4 flat_gap_hours default = 48h** defines "long flat/stale window." The
  spec says "long flat/stale window" / "languishes ... runs weeks later"
  without a number; 48h is conservative relative to the 7d window. Reviewer
  should consider whether it should scale with window_hours.
- **DD-5 flat_band_pct default = +/-10%** defines "flat." Interacts with DD-2:
  band must be < run_threshold or a "flat" window could itself contain a run.
- **DD-6 min_points default = 5** doubles as the distribution-null threshold
  (reference N<5 convention). At sparse CG cadence, a 7d window may legitimately
  have few points; tune jointly with the cadence.
- **DD-7 maturity_hours default = window_hours.** Guardrail #3 ("lookback
  maturity for all cohorts"): a candidate is only classifiable once its full
  post-detection window has elapsed. Reviewer: confirm maturity == full window
  (strict) vs a shorter partial-maturity allowance.
- **DD-8 lookback_days default = 30** for cohort selection (which detections to
  audit). Independent of the 7d price-window ceiling.
- **DD-9 cohort default = both** (paper + gainers). Reviewer: confirm both
  ledgers are in scope, or restrict to the gainers/runner source the operator
  cares about.
- **DD-10 (decision-tree ordering):** `unrelated_later_move` is evaluated
  before the dip-depth split (§2.4). Confirm the flat-gap classification
  should dominate excursion shape (design reading of "do not count as a live
  false negative").
- **DD-11 (P0 source priority):** ledger `detected_price`/`entry_price` first,
  else first valid in-window price point. Confirm this priority; an alternative
  is "always use first in-window price" for consistency across both ledgers.
- **DD-12 (peak tie-break):** `time_to_peak` uses the FIRST point achieving the
  max price (earliest peak). Confirm earliest-peak vs last-peak semantics.
- **DD-13 (points_distribution inclusion):** whether to additionally emit a
  per-token in-window point-count distribution (reference-style, null<5). Low
  cost; included as optional in §5. Confirm keep/drop.

---

## 7. File deliverables (when this design is approved — NOT in this PR)

- `scripts/audit_clean_price_path.py` (pure `build_report` + `main()`,
  read-only, offline banner).
- `tests/test_audit_clean_price_path.py` (cases in §5).

No other files change. `backlog.md` is intentionally NOT modified by this
design pass.

---

## 8. Review folds applied (two orthogonal APPROVE-WITH-FOLDS reviews)

1. **Scope-limitation honesty** — added the top-of-body SCOPE-LIMITATION
   section: `volume_history_cg` 7-day retention means genuine weeks-later
   catalysts are UNOBSERVABLE; within their 7-day window they did not run, so
   they land in `no_significant_move`, NOT `window_incomplete`/
   `insufficient_data`. `unrelated_later_move` captures only flat-then-run
   ENTIRELY WITHIN the 7-day window. Weeks-later recurrence is out of V1 scope.
2. **Timestamp semantics reconciled** — series lower bound pinned as
   `recorded_at >= detection_ts` (INCLUSIVE of the detection instant);
   prose/pseudocode/guardrail wording unified to "at-or-after detection"
   (removed "strictly after"). §2.2, §4 item 5.
3. **`unrelated_later_move` retains metrics** — only `insufficient_data` and
   `window_incomplete` null `mfe`/`mae`/`time_to_peak`; `unrelated_later_move`
   keeps non-null metrics. §2.5 + `test_unrelated_later_move_retains_metrics`.
4. **Join-failure breakdown split by source/chain** — `join_failure_breakdown`
   field splits `insufficient_data` by cohort source and identity class so an
   enriched bucket for one class is visible, not silent. The
   contract-address-does-not-join behavior is established by a NEW test, not by
   mis-citing the reference's test. §3.5.
5. **Gainers runner-def crosscheck** — for the gainers cohort, emit BOTH the
   audit's computed MFE and the stored `peak_gain_pct` (CONFIRMED present in
   the `gainers_comparisons` DDL alongside `peak_price`), plus disagreement
   counts in BOTH directions, supporting operator ratification of the canonical
   runner def. §3.6.
6. **Cross-arg validation `flat_band_pct < run_threshold`** — exit 2 if a
   'flat' band is not strictly narrower than a 'run', same exit-2 discipline as
   the window-hours bound. §1 + `test_main_rejects_flat_band_ge_run_threshold`.
7. **Threshold sensitivity sweep** — optional `--sensitivity` flag adds a
   `sensitivity` block recomputing per-bucket counts over
   run_threshold∈{20,30,40} × drawdown∈{10,15,20} with per-bucket count
   stability ranges; default off, but tested. §3.7.
8. **Matured-rate block N<5 suppression** — null the WHOLE
   `bucket_rates_matured` block when the matured denominator < 5 (reference
   N<5 convention at the rate-denominator level), with explicit
   `matured_denominator` + suppression-reason fields. §2.5/§3.1/§3.3.

---

## 8b. Fold round 2 (post-code-review: 2 subagents APPROVE-WITH-FOLDS, Codex BLOCK)

A second review round after the implementation landed (commit `fdc18ac`)
produced one Codex BLOCK plus two APPROVE-WITH-FOLDS. The following six folds
were applied (TDD: test pinned first, then implementation). All 44 tests pass.

1. **[Codex CRITICAL — silent sqlite failure]** `_price_series` previously did
   `except sqlite3.Error: return []`, so a missing/renamed `volume_history_cg`
   (or any query failure) masqueraded as `insufficient_data` with exit 0 — a
   silent failure. Fixes:
   - **Schema precondition** added in `main()` BEFORE `build_report`: verify the
     `volume_history_cg` table exists and has columns `coin_id`, `price`,
     `recorded_at` (via new `_table_exists` + existing `_column_exists`). If
     absent → `{"status":"error","stage":"schema",...}` (JSON) / stderr (human)
     and **exit 2**. Pinned by `test_main_exit2_when_price_table_missing` and
     `test_main_exit2_when_price_column_missing`.
   - The bare `except sqlite3.Error: return []` was **removed** from
     `_price_series`; a top-level `try/except sqlite3.Error` around
     `build_report` in `main()` now maps any residual query-time error to
     `{"stage":"query"}` / **exit 2**. `insufficient_data` is now reserved
     STRICTLY for "schema OK but this token has < `min_points` valid in-window
     points." Pinned by `test_main_query_time_sqlite_error_surfaces_exit2`.
2. **[Codex IMPORTANT — maturity guard]** `main()` now rejects
   `--maturity-hours < --window-hours` with `{"stage":"args"}` / exit 2 (a
   candidate is only classifiable once its full post-detection window has
   elapsed). Default `maturity_hours == window_hours` still satisfies this.
   Pinned by `test_main_rejects_maturity_below_window`.
3. **[Both subagents IMPORTANT + Codex NIT — mae_before_favorable semantics]**
   MAE is now the dip BEFORE the FIRST point that crosses `run_threshold`
   (`first_run_dt`), not the dip before the GLOBAL peak — this is the entry
   drawdown the operator had to tolerate before the run started, which is what
   separates `continuous_move` from `drawdown_then_recovery`. Floored at 0.0
   (never negative). `mfe` and `time_to_peak` still use the GLOBAL peak. When no
   in-window run crossing exists (`no_significant_move`), MAE falls back to the
   pre-peak trough. Pinned by `test_mae_uses_first_run_crossing_not_global_peak`
   (path 100→95→130→80→200 classifies `continuous_move`, MAE=5% not 20%) and
   `test_mae_floored_at_zero_when_no_pre_run_dip`.
4. **[Statistical subagent IMPORTANT — gainers crosscheck horizon artifact]** In
   `_gainers_crosscheck`, rows where `audit_mfe is None` (unjoinable / no
   in-window series) are now counted in a separate `audit_unjoinable` counter
   instead of being folded into `disagree_audit_no_stored_yes`. A `caveat` field
   notes that stored `peak_gain_pct` uses full lifetime while audit MFE uses the
   ≤7-day window. Pinned by
   `test_gainers_crosscheck_unjoinable_not_counted_as_disagreement`.
5. **[Statistical subagent IMPORTANT — scope limitation in OUTPUT]** The report
   dict now carries a `scope_limitation` string and `retention_ceiling_hours`
   (168) stating weeks-later catalysts land in `no_significant_move` (not
   `window_incomplete`) within the 7-day retention. Travels in BOTH the JSON
   payload and the human output (not only the docstring). Pinned by
   `test_report_includes_scope_limitation`.
6. **[NITs]** Renamed `zero_in_window_points` → `below_min_points_with_ledger_p0`
   (with a comment clarifying it covers 1..`min_points`-1, not strictly zero).
   Added a comment that `peak_dt` uses earliest-occurrence tie-break
   intentionally. Reconciled the stale Hermes-first row claiming reuse of the
   reference's `_quantile` / `_points_distribution` helpers (those were dropped;
   only `_rate_or_null` is used).
