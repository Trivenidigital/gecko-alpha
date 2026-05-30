**New primitives introduced:** NONE

<!--
  Scope: DIAGNOSTIC-ONLY (Step 1) slice of BL-NEW-FOCUS-FRESHNESS-TRADABILITY-GATES.
  This document designs a READ-ONLY audit script. It introduces no new runtime
  primitives, no new endpoint, no new DB table, no config keys consumed by the
  live pipeline. The script reuses the existing /api/todays_focus endpoint and
  mirrors the shipped scripts/audit_price_path_coverage.py conventions exactly.
-->

# Design — Today's Focus Freshness / Tradability Gate Audit (diagnostic-only)

- **Backlog item:** BL-NEW-FOCUS-FRESHNESS-TRADABILITY-GATES (Step 1 of 3 only)
- **Author date:** 2026-05-29
- **Worktree / branch:** `C:/projects/gecko-alpha-wt/focus-freshness` on `feat/focus-freshness-tradability-audit` (base `origin/master` @ `b1e1c752`)
- **Deliverable of THIS design:** one script `scripts/audit_focus_freshness_tradability.py` + one test `tests/test_audit_focus_freshness_tradability.py`. (This document specifies them; it does NOT write them.)

## 0. Scope boundary (HARD)

The full backlog item has three steps:

1. **(IN SCOPE — this design)** Read-only AUDIT: for each candidate factual gate,
   count how many Today's Focus rows it would exclude and how many of the current
   top-5 it would remove. Diagnostics only, no behaviour change.
2. **(OUT OF SCOPE)** Apply live pre-curation filters to Today's Focus. PIPELINE-
   AFFECTING. Not designed, not touched here.
3. **(OUT OF SCOPE)** Anything downstream of step 2.

This audit answers exactly one question per gate: *"If we applied this factual
filter, how many Focus rows and how many current top-5 rows would drop out?"* —
and nothing else. It does **not** rank, re-order, curate, write, or change the
endpoint. See §6 (Anti-scope contract) for the enforceable statement.

> **Provenance note (RESOLVED).** The operator's canonical spec
> `C:\projects\gecko-alpha\.spec_focus_fresh.txt` was read and is **non-empty**: it
> reproduces the backlog entry verbatim (goal, required drift/runtime checks,
> candidate build shape, anti-scope). The backlog DOES contain
> `BL-NEW-FOCUS-FRESHNESS-TRADABILITY-GATES` at `backlog.md:69` (Track 1) and the
> full entry at `backlog.md:136-153`. The gate list, thresholds, and anti-scope
> rules below are therefore **operator-specified** (from spec/backlog), not author
> guesses. Specifically the spec's candidate thresholds are: **stale price > 24h,
> missing detected price, age > 12h, move from detection > 150%, no deterministic
> chart/venue route, liquidity unavailable** — these are the defaults adopted in §4.

## Hermes-first analysis

Checked the Hermes skill hub (`hermes-agent.nousresearch.com/docs/skills`) plus
the awesome-hermes-agent ecosystem for any skill/library that already covers the
capability this script needs.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Data quality / freshness gating diagnostics | none found | Build from scratch — this is a project-specific read-only counter over an in-repo HTTP endpoint with project-defined gate semantics; no generic skill substitutes |
| Counterfactual "rows excluded by filter X" reporting | none found | Build from scratch — trivial pure-Python counting; a skill would be heavier than the code |
| HTTP/JSON fetch of a localhost endpoint | none found (and not warranted) | Use stdlib `urllib` exactly as the shipped reference `audit_price_path_coverage.py` does; no dependency, mirrors house convention |
| Top-N "would this row be removed" analysis | none found | Build from scratch — depends on this endpoint's own ordering, not a portable concept |

**awesome-hermes-agent ecosystem check + verdict:** Scanned the ecosystem list
for data-quality / freshness / counterfactual-filter / dashboard-audit entries;
the closest categories are agent-orchestration and chain-data skills, none of
which apply to an offline read-only diagnostic over a local FastAPI endpoint.
**Verdict: none apply — build from scratch.** The capability is ~150 LOC of
pure counting that must mirror an already-shipped in-repo audit script's
conventions; introducing a Hermes dependency would be net-negative.

## 1. Inputs — existing endpoint fields ONLY

The script consumes the **existing** `GET /api/todays_focus?window_hours=N`
endpoint over `urllib` (identical transport to the shipped reference). It reads
facts from fields **already present on each row** and computes **no new
detection facts**. It must not change the endpoint or add fields.

### 1a. Field-name confirmation status (VERIFIED against the live row builder)

STEP 3 of the task asked to confirm the exact field names against the
`/api/todays_focus` row builder. **These have now been read and VERIFIED** in
`dashboard/db.py` (`_today_focus_row`, db.py:1978-2070 with the emitted dict at
2032-2061, which builds each focus row) and `dashboard/api.py`
(`get_todays_focus`, api.py:438-456, which returns the
`db.get_todays_focus()` dict as-is — no Pydantic `response_model`). The endpoint
envelope is `{"meta": {...}, "rows": [...]}` (db.py:2230 returns
`{"meta": meta, "rows": rows}`). The script reads `payload["rows"]`.

VERIFIED keys on each focus row relevant to the gates. The authoritative source
is `_today_focus_row(row)` at **db.py:1978-2070** (the `out = {...}` dict it
returns is db.py:2032-2061), which returns the dict emitted for each focus row
(the endpoint returns `db.get_todays_focus()` as-is, with NO Pydantic
`response_model`, api.py:438-456). The emitted key set is EXACTLY the 28 keys in
that dict — corroborated independently by the strict, exhaustive
`EXPECTED_ROW_KEYS` frozenset in `scripts/check_todays_focus_contract.py:53-84`
(any key not in that set fails the contract's "unknown keys" critical check).

| Gate | VERIFIED field on the row | Status | Fact used |
|---|---|---|---|
| stale price | `price_staleness_minutes` (number/None) primary; `price_is_stale` (bool) fallback | **PRESENT** (db.py:2050-2051) | price considered stale beyond `--stale-hours` |
| missing detected price | `current_move_pct is None` proxy — no `entry_price`/`detected_price` key | **PROXY (see note A)** | no recorded detection price |
| too old since detection | `opened_age_hours` (number/None) | **PRESENT** (db.py:2045) | hours since the row entered Focus |
| far-moved-from-detection | `current_move_pct` (number/None; == `pct_from_entry`) | **PRESENT** (db.py:2052) | move vs detection/entry price |
| no venue route | `chart_url` — *(no such key on the focus row)* | **ABSENT → NOT-EVALUABLE** (see note B) | no tradeable venue/chart link |
| liquidity unavailable | `liquidity_usd` — *(no field on the focus row)* | **ABSENT → NOT-EVALUABLE** (see note C) | liquidity datum missing |
| block reason (informational) | `block_reason_primary` / `block_cause` | **PRESENT** (db.py:2059-2060) | already-flagged block; cross-tab only, NOT a gate |

**Note A — there is NO `entry_price`/`detected_price` on the focus row.**
`_today_focus_row` (db.py:1978-2070, dict at 2032-2061) emits no entry/detection
price key. What the focus row DOES carry is `current_move_pct`, populated at
db.py:2052 as `row.get("pct_from_entry")`, which is `None` **exactly** when entry or
current price was missing/invalid (db.py:1392-1398). Therefore the
`missing_detected_price` gate is evaluated **via `current_move_pct is None`** — a
`None` `current_move_pct` is the observable signal that detection/entry price was
missing — NOT via a non-existent `entry_price` key. If `entry_price` is later added
to the surface the gate can switch to it. **This is a verified correction to the
task-brief candidate field name `detected_price` — that field is not on this
surface.** Flagged in §7.

**Note B — `chart_url` is NOT emitted on the focus row → gate NOT-EVALUABLE.**
The authoritative row builder `_today_focus_row` (db.py:1978-2070, dict at 2032-2061) emits NO
`chart_url` (or any venue/route) key, and the strict contract checker's
`EXPECTED_ROW_KEYS` (check_todays_focus_contract.py:53-84) contains no such key —
were one present the endpoint would fail the contract's "unknown keys" check. There
is therefore **no per-row deterministic chart/venue field to evaluate**. Exactly as
the sibling design found, the `no_venue_route` gate is structurally
**not-evaluable**: it returns `None` for every row → `evaluable_count: 0`,
`excluded_rate: null`, and `field_findings.no_venue_route.rows_missing_rate == 1.0`.
That 100%-missing IS the deliverable's finding for this gate; the gate is reported
in `field_findings`, NOT silently treated as pass (which would falsely claim "all
rows have a venue route") or fail (which would falsely exclude every row). See §7.

**Note C — no liquidity field on the focus row → gate NOT-EVALUABLE.**
`_today_focus_row` (db.py:1978-2070, dict at 2032-2061) emits no liquidity key (`liquidity_usd` exists
on `candidates` at db.py:55 but is not joined into the focus surface; it is also
absent from `EXPECTED_ROW_KEYS`). The `liquidity_unavailable` gate is therefore
structurally **not-evaluable**; it reports `evaluable_count: 0`, `excluded_rate:
null`, and `field_findings.liquidity_unavailable.rows_missing_rate == 1.0`. That
100%-missing IS the deliverable's finding for this gate. (The shipped
`scripts/audit_liquidity_coverage.py` audits liquidity at the DB layer — a
different, complementary surface.)

> **Decision flagged:** `block_reason_primary`/`block_cause` is treated as an
> **informational cross-tabulation dimension**, not an independent factual gate,
> because it is a downstream curation verdict, not a raw fact. Surfacing it as a
> gate would re-derive curation logic, which is out of scope. See §7.

### 1b. Top-N definition

"Current top-5" = the **first 5 rows in endpoint return order**. The audit makes
**no assumption about and no change to** how the endpoint orders rows — it simply
takes `rows[:TOP_N]` with `TOP_N = 5`. If the endpoint returns fewer than 5 rows,
`topN_removed` counts are computed over the rows actually present (denominator =
`min(5, total_rows)`), and rates are null-when-denominator-0 (§2b). `TOP_N` is a
module constant, overridable via `--top-n` for forward-proofing, default 5.

## 2. Pure core: `build_report(...)`

Mirrors the reference's pure-core shape exactly: an injectable `now`, no I/O
inside, returns a JSON-serialisable dict.

```python
def build_report(
    endpoint_url: str,
    rows: list[dict],
    now: datetime,                       # injected, tz-aware UTC
    *,
    stale_hours: float,                  # staleness threshold (price)
    max_age_hours: float,                # too-old-since-detection threshold
    max_move_pct: float,                 # far-moved-from-detection threshold (abs %)
) -> dict:
    ...
```

### 2a. Per-gate evaluation

Each gate is a pure predicate `excluded(row) -> bool | None`:
`True`  = row WOULD be excluded by this gate,
`False` = row survives this gate,
`None`  = required field absent → cannot evaluate → counted into `field_findings`,
NOT into `excluded_count` and NOT into the survivor set (treated as "unknown",
documented, see §7 decision on conservative handling).

Gate predicates use the VERIFIED row keys from §1a (read existing fields only):

```text
gate "stale_price":                              # field: price_staleness_minutes
    if "price_staleness_minutes" not in row: excluded = None (field_findings)
    elif row["price_staleness_minutes"] is None:
        b = row.get("price_is_stale")            # bool fallback (server 60-min def)
        excluded = bool(b) if b is not None else None
    else: excluded = (row["price_staleness_minutes"] >= stale_hours * 60)

gate "missing_detected_price":                   # field: current_move_pct (§1a note A)
    if "current_move_pct" not in row: excluded = None (field_findings)
    else: excluded = (row["current_move_pct"] is None)
    # current_move_pct is None EXACTLY when entry/current price was missing/invalid
    # (db.py:1393-1398). entry_price/detected_price are NOT on the focus row.

gate "too_old_since_detection":                  # field: opened_age_hours
    a = row.get("opened_age_hours", MISSING)
    if a is MISSING or a is None: excluded = None (field_findings)
    else: excluded = (a > max_age_hours)

gate "far_moved_from_detection":                 # field: current_move_pct
    m = row.get("current_move_pct", MISSING)
    if m is MISSING or m is None: excluded = None (field_findings)
    else: excluded = (abs(m) > max_move_pct)

gate "no_venue_route":                           # field: chart_url (ABSENT, §1a note B)
    if "chart_url" not in row: excluded = None (field_findings)
    else: excluded = (row["chart_url"] is None or row["chart_url"] == "")
    # chart_url is NOT emitted on the focus row (db.py:2032-2061) and is absent from
    # the contract's EXPECTED_ROW_KEYS -> this gate is not-evaluable on EVERY row ->
    # excluded(row) is None for all rows -> evaluable_count 0, excluded_rate null,
    # rows_missing_rate 1.0. Reported in field_findings, never silently pass/fail.

gate "liquidity_unavailable":                    # field: liquidity_usd (ABSENT, §1a note C)
    if "liquidity_usd" not in row: excluded = None (field_findings)
    else: excluded = (row["liquidity_usd"] is None)  # "unavailable" = datum missing,
                                         # NOT a threshold on amount (that would be
                                         # ranking/curation -> out of scope)
    # liquidity_usd is NOT on the focus row today -> this gate is not-evaluable on
    # every row -> evaluable_count 0, excluded_rate null, rows_missing_rate 1.0.
```

> **`current_move_pct` does double duty.** `missing_detected_price` keys on
> `current_move_pct is None` (the observable proxy for "no entry/detection price",
> §1a note A); `far_moved_from_detection` keys on its *magnitude* when present.
> The two are disjoint per row. Reviewer to confirm (§7).

> **Boundary convention (decision flagged):** thresholds use `>` (and `>=` for
> minute-based staleness) — a row exactly AT the threshold is **kept** for
> age/move, and a row AT-or-over the staleness-minutes threshold is **excluded**.
> Documented explicitly so tests can pin the boundary. See §5 boundary cases and
> §7 ambiguity #4.

### 2b. Counting + rates

For each gate over the FULL cohort:

```text
excluded_count   = number of rows with excluded(row) is True
evaluable_count  = number of rows with excluded(row) in (True, False)   # not None
excluded_rate    = _rate(excluded_count, evaluable_count)   # null when denom 0
topN_removed     = number of rows in rows[:TOP_N] with excluded(row) is True
topN_removed_rate= _rate(topN_removed, min(TOP_N, total_rows))  # null if gate not_evaluable
```

`_rate(num, denom)` returns `None` when `denom == 0`, else `num / denom`
(identical helper to the reference — null-rate-when-denom-0 convention).

> **Decision:** `excluded_rate` denominator is `evaluable_count` (rows where the
> gate's field exists), not `total_rows`. Dividing by `total_rows` would understate
> a gate's effect whenever the field is sometimes absent and conflate "field
> missing" with "row survives". `field_findings` separately reports absence counts
> so both numbers are visible. Flagged §7 ambiguity #3.

### 2c. Combined survivors (rows surviving ALL gates)

```text
A row "survives all gates" iff for EVERY gate, excluded(row) is False.
  - excluded(row) is True  -> row dropped
  - excluded(row) is None  -> field missing; row is NOT counted as a survivor
                              (conservative: cannot prove it passes). It is
                              tallied in combined.unknown_rows and detailed in
                              field_findings so the operator sees why.
combined.survivors_count   = count of rows surviving all gates
combined.survivors_rate    = _rate(survivors_count, total_rows)
combined.dropped_count     = total_rows - survivors_count
combined.top5_survivors    = count in rows[:TOP_N] surviving all gates
combined.unknown_rows      = count of rows with >=1 gate == None
```

### 2d. Schema / field findings

```text
field_findings = {
   "<gate_name>": {
       "field_checked": "<field or 'price_is_stale|price_staleness_minutes'>",
       "rows_missing_field": <int>,           # excluded(row) was None for this gate
       "rows_missing_rate": _rate(rows_missing_field, total_rows),
   }, ...
}
schema_findings = sorted list of human-readable strings, e.g.
   "gate 'far_moved_from_detection': field 'pct_from_entry' absent on 3/40 rows"
```

These two surfaces are how a missing/renamed field is **reported rather than
crashed on** — directly satisfying the task requirement and §1a uncertainty.

## 3. Output JSON shape

Mirrors the reference (`audited_at` Z, `params`, totals, per-gate, findings):

```json
{
  "audited_at": "2026-05-29T14:03:00Z",
  "endpoint": "http://127.0.0.1:8000/api/todays_focus?window_hours=48",
  "params": {
    "window_hours": 36,
    "stale_hours": 24.0,
    "max_age_hours": 12.0,
    "max_move_pct": 150.0,
    "top_n": 5
  },
  "total_rows": 5,
  "per_gate": {
    "stale_price":            {"excluded_count": 1, "evaluable_count": 5, "excluded_rate": 0.2, "topN_removed": 1, "topN_removed_rate": 0.2, "status": "evaluable"},
    "missing_detected_price": {"excluded_count": 0, "evaluable_count": 5, "excluded_rate": 0.0,  "topN_removed": 0, "topN_removed_rate": 0.0, "status": "evaluable"},
    "too_old_since_detection":{"excluded_count": 2, "evaluable_count": 5, "excluded_rate": 0.4, "topN_removed": 2, "topN_removed_rate": 0.4, "status": "evaluable"},
    "far_moved_from_detection":{"excluded_count": 1,"evaluable_count": 4, "excluded_rate": 0.25, "topN_removed": 0, "topN_removed_rate": 0.0, "status": "evaluable"},
    "no_venue_route":         {"excluded_count": 0, "evaluable_count": 0, "excluded_rate": null, "topN_removed": null, "topN_removed_rate": null, "status": "not_evaluable"},
    "liquidity_unavailable":  {"excluded_count": 0, "evaluable_count": 0, "excluded_rate": null, "topN_removed": null, "topN_removed_rate": null, "status": "not_evaluable"}
  },
  "combined": {
    "survivors_count": 1,
    "survivors_rate": 0.2,
    "dropped_count": 4,
    "topN_survivors": 1,
    "unknown_rows": 1,
    "malformed_rows": 0
  },
  "field_findings": {
    "no_venue_route": {"field_checked": "chart_url", "rows_missing_field": 5, "rows_missing_rate": 1.0},
    "liquidity_unavailable": {"field_checked": "liquidity_usd", "rows_missing_field": 5, "rows_missing_rate": 1.0},
    "missing_detected_price": {"field_checked": "current_move_pct", "rows_missing_field": 0, "rows_missing_rate": 0.0},
    "far_moved_from_detection": {"field_checked": "current_move_pct", "rows_missing_field": 1, "rows_missing_rate": 0.20}
  },
  "schema_findings": [
    "gate 'no_venue_route': field 'chart_url' absent on 5/5 rows (not on the focus surface) -> NOT-EVALUABLE",
    "gate 'liquidity_unavailable': field 'liquidity_usd' absent on 5/5 rows (not on the focus surface) -> NOT-EVALUABLE",
    "gate 'far_moved_from_detection': field 'current_move_pct' was None on 1/5 rows (entry/current price missing)"
  ]
}
```

> **Realistic shape today.** The focus endpoint caps at 5 rows (`max_rows = 5`),
> so `total_rows <= 5` and "top-5" == the whole cohort in practice. **TWO of the
> six gates are structurally NOT-EVALUABLE today** because their backing field is
> not on the focus surface: `no_venue_route` (no `chart_url` key, §1a note B) and
> `liquidity_unavailable` (no `liquidity_usd` key, §1a note C). Both report
> `evaluable_count: 0`, `excluded_rate: null`, `rows_missing_rate: 1.0` — that is
> the finding, not a pass. The gates that actually bite are
> `stale_price` / `too_old_since_detection` / `far_moved_from_detection`, plus
> `missing_detected_price` via the `current_move_pct is None` proxy.
```

`audited_at` is `now.strftime("%Y-%m-%dT%H:%M:%SZ")` with injected UTC `now`
(reference convention). All rates are float-or-null.

## 4. CLI — `main(argv=None)`

argparse, mirroring the reference's exit-code discipline (0 success / 2 error):

| Flag | Type | Default | Validation |
|---|---|---|---|
| `--url` | str | `http://127.0.0.1:8000` | base only; endpoint path appended by script |
| `--window-hours` | int | 36 | `6 <= n <= 72` else exit 2 (matches endpoint `Query(36, ge=6, le=72)`, api.py:440) |
| `--stale-hours` | float | **24.0** [operator-specified: spec "stale price > 24h"] | must be > 0 else exit 2 |
| `--max-age-hours` | float | **12.0** [operator-specified: spec "age > 12h"] | must be > 0 else exit 2 |
| `--max-move-pct` | float | **150.0** [operator-specified: spec "move from detection > 150%"] | must be > 0 else exit 2 |
| `--top-n` | int | 5 | must be > 0 else exit 2 |
| `--timeout` | float | 10.0 | must be > 0 else exit 2 (mirrors reference) |
| `--json` | flag | off | when set, print ONLY the JSON dict (no human summary) |

**All three numeric gate thresholds are operator-specified** in the spec's
candidate-threshold list (`.spec_focus_fresh.txt` line 10 / `backlog.md:145`), not
author defaults. `--window-hours 36` matches the endpoint default + bounds; the
script validates `[6, 72]` locally so an out-of-range value yields a clean `args`
error instead of a server 4xx surfacing as a fetch failure.

`main()` flow (mirrors reference):

```text
1. parse args; validate; on bad arg -> print error to stderr, return 2
2. endpoint_url = f"{url}/api/todays_focus?window_hours={window_hours}"
3. try: rows = _fetch_focus_rows(endpoint_url)
   except Exception as e: print error to stderr, return 2     # fetch failure
4. report = build_report(endpoint_url, rows, now=datetime.now(timezone.utc), **thresholds)
5. if args.json: print(json.dumps(report, indent=2))
   else: print human-readable summary THEN json.dumps(report)
6. return 0
```

Exit codes: `0` clean; `2` any of {bad/invalid args, fetch/transport/JSON-decode
failure}. No other codes. `if __name__ == "__main__": sys.exit(main())`.

`_fetch_focus_rows(endpoint_url)` — stdlib `urllib.request.urlopen` with a
timeout, `json.loads(resp.read())`, returns the row list; raises on HTTP/decode
error (caught in `main` → exit 2). Identical transport to the shipped reference;
monkeypatched in tests.

## 5. TDD test plan — `tests/test_audit_focus_freshness_tradability.py`

Mirrors reference fixture style: load the script module via
`importlib.util.spec_from_file_location` + `SourceFileLoader`; module-level
`FIXED_NOW = datetime(2026, 5, 29, 14, 0, 0, tzinfo=timezone.utc)`; build rows as
dicts; call `build_report(... now=FIXED_NOW ...)`; monkeypatch
`mod._fetch_focus_rows` for `main()` tests.

Concrete cases:

**Per-gate exclude/keep + boundary (one pair each):**
1. `stale_price` — row with `price_is_stale=True` excluded; `False` kept;
   minute-variant: `price_staleness_minutes == stale_hours*60` excluded (>=),
   one minute under kept.
2. `missing_detected_price` — row with `current_move_pct=None` excluded; row with a
   real `current_move_pct` number kept. (Verified proxy — §1a note A; there is no
   `detected_price` field on the focus row.)
3. `too_old_since_detection` — `opened_age_hours = max_age_hours + 0.1` excluded;
   `== max_age_hours` kept (boundary, `>` semantics).
4. `far_moved_from_detection` — `current_move_pct = max_move_pct + 0.1` excluded;
   `-max_move_pct` (negative, equal magnitude at boundary) kept; `-(max+0.1)`
   excluded (abs); `current_move_pct=None` → not-evaluable (counted in
   field_findings, NOT excluded) — pin the None-vs-excluded distinction.
5. `no_venue_route` — **NOT-EVALUABLE on the real surface** (no `chart_url` key,
   §1a note B). Real-world case: a row with NO `chart_url` key → gate returns None
   → `evaluable_count == 0`, `excluded_rate is None`,
   `field_findings.no_venue_route.rows_missing_rate == 1.0`, and a `schema_findings`
   line is emitted. Pin BOTH the predicate behaviour for forward-proofing (if the
   field is ever added: `chart_url=None`/`""` → excluded; non-empty URL → kept) AND
   the today-reality not-evaluable path — the not-evaluable path is the one that
   matches the live endpoint.
6. `liquidity_unavailable` — row with NO `liquidity_usd` key → gate returns None
   (not-evaluable), `evaluable_count==0`, `excluded_rate is None`,
   `field_findings.liquidity_unavailable.rows_missing_rate==1.0`; a row WITH
   `liquidity_usd=None` → excluded; with a number → kept. (Verifies the absent-field
   path that is the real-world shape today — §1a note C.)

**Top-5 removal counting:**
7. Construct 7 rows where rows[0] and rows[2] are stale → `stale_price.top5_removed
   == 2`, `top5_removed_rate == 0.4`; assert rows[5]/rows[6] (outside top-5) do
   not affect top5 counts but DO affect cohort `excluded_count`.
8. Endpoint returns 3 rows total → `top5_removed_rate` denominator = 3 (min(5,3)),
   not 5.

**Combined survivors:**
9. Mixed cohort → `combined.survivors_count` equals rows failing zero gates;
   `dropped_count == total - survivors`; a row excluded by 2 gates counted once
   in dropped.

**Missing field surfaced, not crashed:**
10. Row lacking `pct_from_entry` → no exception; that gate's `excluded(row)` is
    None → `field_findings["far_moved_from_detection"].rows_missing_field == 1`;
    row appears in `combined.unknown_rows`; `schema_findings` has the line.
11. Entire field absent on ALL rows → gate `evaluable_count == 0`,
    `excluded_rate is None` (null-when-denom-0).

**Rate null when cohort empty:**
12. `rows == []` → `total_rows == 0`; every `excluded_rate`,
    `top5_removed_rate`, `survivors_rate` is `None`; no exception;
    `audited_at` present.

**`main()` exit-code paths:**
13. `main(["--window-hours", "0"])` → returns 2 (invalid arg), stderr nonempty.
14. `main(["--stale-hours", "-1"])` → returns 2.
15. `_fetch_focus_rows` monkeypatched to raise → `main([])` returns 2.
16. `_fetch_focus_rows` monkeypatched to return a valid 2-row list → `main([])`
    returns 0; `main(["--json"])` returns 0 and stdout is parseable JSON with
    `total_rows == 2`.

**Determinism / shape:**
17. `audited_at` uses injected `now` → equals `FIXED_NOW.strftime("%Y-%m-%dT%H:%M:%SZ")`.
18. `params` echoes all thresholds + `top_n` + `window_hours`.

## 6. Anti-scope contract (ENFORCEABLE)

Per global CLAUDE.md memory *"Anti-scope as runtime contract"* — the boundary is
expressed so a reviewer/checker can mechanically verify it. The script ships with
a docstring contract block AND the test file asserts these properties, so the
boundary is checkable, not aspirational:

```text
ANTI-SCOPE CONTRACT (verifiable):
  C1  NO writes:        the module imports neither sqlite3/aiosqlite nor any
                        db writer; it opens NO file for writing; it performs
                        NO HTTP method other than the single GET in
                        _fetch_focus_rows. -> test greps module source for
                        forbidden tokens (open(...,'w'), 'INSERT', 'UPDATE',
                        'DELETE', '.commit(', 'requests.post', urlopen with data=).
  C2  NO ranking:       build_report never re-orders rows; it consumes endpoint
                        order as-is and only COUNTS. -> test asserts the function
                        returns no ordered list of rows, only counts/rates.
  C3  NO curation change:the script does not import dashboard curation modules and
                        produces output to stdout only. -> test asserts no import
                        of dashboard.* curation symbols.
  C4  NO new endpoint:  reuses GET /api/todays_focus only; defines no route,
                        no FastAPI app. -> test greps for absence of @app/@router.
  C5  READ-ONLY facts:  reads only pre-existing row fields; computes NO new
                        detection fact (no price recompute, no venue lookup,
                        no liquidity fetch). -> reviewed at PR; predicates in §2a
                        read row.get(...) only.
```

The deliverable is **diagnostic output only**: numbers that tell the operator
what each factual gate WOULD remove. Acting on those numbers (Step 2) is a
separate, out-of-scope, pipeline-affecting change.

## 7. Edge cases & decisions flagged for review

1. **[RESOLVED] Spec + backlog present.** `.spec_focus_fresh.txt` is non-empty and
   reproduces the backlog entry; the backlog item exists at `backlog.md:69` /
   `:136-153`. Gate list, thresholds (24h / 12h / 150%), and anti-scope are
   operator-specified — not author guesses. No action needed.
2. **[RESOLVED — with corrections] Field names VERIFIED against the live row
   builder** (`_today_focus_row`, db.py:1978-2070, dict at 2032-2061 — the authoritative dict the
   endpoint returns as-is, api.py:438-456, no `response_model`), corroborated by the
   strict `EXPECTED_ROW_KEYS` in check_todays_focus_contract.py:53-84. Confirmed
   PRESENT: `price_staleness_minutes`, `price_is_stale`, `opened_age_hours`,
   `current_move_pct` (== `pct_from_entry`, db.py:2052), `move_basis`,
   `block_reason_primary`, `block_cause`. **Three corrections to the brief's
   candidate names — two gates are NOT-EVALUABLE:** (a) NO `detected_price`/
   `entry_price` on the focus row → `missing_detected_price` keys on
   `current_move_pct is None` instead (§1a note A); (b) **NO `chart_url`/venue key
   on the focus row → `no_venue_route` is NOT-EVALUABLE** (§1a note B) — this
   corrects the earlier draft claim that `chart_url` was "almost always populated",
   which was wrong; the field is simply absent; (c) NO liquidity field on the focus
   row → `liquidity_unavailable` is NOT-EVALUABLE (§1a note C). Both not-evaluable
   gates report via `field_findings`, never silently pass/fail. These are the
   highest-value reviewer items.
3. **Rate denominator = evaluable vs total.** §2b divides `excluded_count` by
   `evaluable_count` (rows where the field exists). Alternative is `total_rows`.
   Chose evaluable to avoid conflating "field missing" with "row survives";
   absence is reported separately. Confirm operator wants it this way.
4. **Boundary semantics.** `>` for age/move (AT-threshold kept), `>=` for
   staleness-minutes (AT-threshold excluded). Pinned by tests; confirm direction.
5. **None handling in combined survivors.** A row with any unevaluable gate is NOT
   counted as a survivor (conservative) and is tallied in `unknown_rows`.
   Alternative is to ignore missing gates when deciding survival. Chose
   conservative; confirm.
6. **`liquidity_unavailable` = datum-missing, not amount-threshold.** Treating it
   as "below $X" would be a curation/ranking decision (out of scope). Kept it as a
   pure presence check. Confirm this matches operator intent (the brief says
   "liquidity unavailable", which reads as presence, not amount).
7. **`block_reason_primary` as cross-tab, not gate.** It is a downstream verdict,
   not a raw fact; treating it as a gate would re-derive curation. Left as an
   optional informational dimension; flagged in case operator wants it counted.
8. **Top-N default 5.** Brief says "current top-5 (or top-N as the endpoint orders
   them)". Default `--top-n 5`; overridable. Endpoint order taken as-is (no
   re-ordering — anti-scope C2).

## 8. Fold round 2 (post code-review, 2026-05-29)

Code review returned one Codex **BLOCK** (1 CRITICAL silent-failure + several
IMPORTANTs) plus two **APPROVE-WITH-FOLDS** subagent reviews. Folds applied to
`scripts/audit_focus_freshness_tradability.py` +
`tests/test_audit_focus_freshness_tradability.py` (TDD: pinning test first):

1. **[CRITICAL — silent empty cohort on shape change]** `_fetch_focus_rows`
   used `payload.get("rows", [])`, so a dict payload WITHOUT a `rows` key (a
   schema change, a `{"data": [...]}` envelope, or a 200 *error* envelope), or
   a non-list `rows` value, silently yielded an empty cohort → exit-0 all-zero
   report — exactly the silent-failure class this diagnostic exists to prevent.
   Extracted `_extract_rows(payload)`: raises `ValueError` (→ `main()` maps to
   exit 2, `stage="fetch"`) when the payload is not a dict, lacks `rows`, or
   `rows` is not a list. A genuine `{"rows": []}` (key present, empty list)
   stays valid → exit 0, `total_rows 0`. Distinction: "`rows` present AND a
   list" (valid, possibly empty) vs "absent OR non-list" (malformed → raise).
   A bare-list payload is no longer silently accepted either. Pinning tests:
   `_extract_rows` unit cases (`{}`, `{"data":...}`, `{"rows": null}`,
   `{"rows": "x"}`, bare list → raise; `{"rows": []}` → `[]`); `main()` end-to-
   end (4 malformed shapes → exit 2 `stage=fetch`; `{"rows": []}` → exit 0).

2. **[IMPORTANT — malformed row element]** A non-dict row element (e.g.
   `{"rows": [null]}`) would crash gate evaluation with an uncaught traceback
   (exit 1). Every gate predicate + the combined survivor pass now skip non-dict
   rows; the count is surfaced as `combined.malformed_rows`, per-gate
   `field_findings[...]["malformed_rows"]`, and a `schema_findings` line
   (surface-and-count — the more diagnostic option). Pinning test:
   `test_non_dict_row_surfaced_not_crashed`.

3. **[IMPORTANT — not-evaluable gate topN rate]** NOT-EVALUABLE gates
   (`no_venue_route`, `liquidity_unavailable`) reported `topN_removed_rate`
   `0.0` even though the gate cannot be evaluated. Both `topN_removed` AND
   `topN_removed_rate` are now `null` for not-evaluable gates — a `0` would
   falsely read as "nothing removed in the top slice" when the truth is "we
   could not look." Evaluable gates keep numeric topN values (regression-
   guarded). Pinning tests: `test_not_evaluable_gates_topn_null`,
   `test_evaluable_gate_topn_still_numeric`.

4. **[IMPORTANT — price_staleness_minutes masking]** When the primary
   `price_staleness_minutes` is absent/None but the `price_is_stale` bool
   fallback fires, the stale gate now surfaces
   `field_findings.stale_price.primary_field_missing_used_bool_fallback` + a
   `schema_findings` line noting `--stale-hours` is bypassed for those rows
   (a bool carries no minutes), rather than silently using the fallback.
   Pinning tests: `test_stale_primary_missing_bool_present_surfaced`,
   `test_stale_primary_present_no_fallback_finding`.

5. **[NIT — malformed numeric value]** Added `_coerce_number`; the numeric
   gates (`opened_age_hours`, `current_move_pct`) treat a present-but-non-
   numeric value (e.g. a string) as missing (surfaced via
   `field_findings[...]["rows_non_numeric"]` + `schema_findings`) instead of
   crashing on the comparison. Pinning test:
   `test_numeric_gates_non_numeric_value_not_crashed`.

6. **[NIT — anti-scope source-grep tests]** §6's promised source-grep anti-
   scope contract is now locked by `MODULE_PATH.read_text()` assertions over a
   comment/docstring-stripped body: no `INSERT`/`UPDATE`/`DELETE`/`.commit(`/
   `.post(`/write-mode `open(...)`; no `dashboard` import; no `@app`/`@router`/
   `FastAPI`/`APIRouter`. The body is stripped so the intentionally-paraphrased
   docstring (per the self-referential-grep lesson) does not trip its own
   scanner. Pinning tests: `test_anti_scope_no_db_writes`,
   `test_anti_scope_no_dashboard_import`, `test_anti_scope_no_web_framework_route`.

7. **[NITs — doc + output consistency]** Reconciled this doc's `top5_*` → the
   implemented generalized `topN_*` keys (incl. the §3 JSON sample, below);
   added a `combined.topN_survivors` value assertion
   (`test_combined_topn_survivors_value`); `_rate_or_null` now rounds to 4 dp
   to match the reference (`test_rate_rounded_to_4dp`).

> **Key-name reconciliation:** the IMPLEMENTED + TESTED keys are
> `topN_removed`, `topN_removed_rate`, and `combined.topN_survivors` (top-slice
> size `min(top_n, total_rows)`); §1b/§2b/§2c/§3 are updated to match. All rates
> are rounded to 4 dp. Not-evaluable gates carry `topN_removed`/
> `topN_removed_rate` as `null` (round-2 fold 3; broadened by round-3 fold B
> below). `combined.malformed_rows` is new (round-2 fold 2).

## 9. Fold round 3 (post second code-review, 2026-05-30)

The re-review of the round-2 HEAD returned two IMPORTANT residual gaps — both
are silent-disclosure / evaluability holes one level deeper than round 2.
Folds applied to `scripts/audit_focus_freshness_tradability.py` +
`tests/test_audit_focus_freshness_tradability.py` (TDD: pinning test first):

**A. [IMPORTANT — non-numeric staleness fallback was silently undisclosed].**
Round 2's fallback DETECTOR (`_stale_uses_bool_fallback`) only flagged the
absent/None primary case: it returned `False` when `price_staleness_minutes`
was *present but non-numeric* (e.g. `"oops"`). But the GATE
(`_gate_stale_price`) routes a non-numeric primary through `_coerce_number`
→ `None` → it DID silently fall back to the `price_is_stale` bool, bypassing
`--stale-hours` with no field-finding surfaced. Detector and gate disagreed.
Fix: replaced the boolean detector with `_stale_fallback_kind(row)` returning
`"missing"` | `"non_numeric"` | `None`, classifying *why* the primary was
unusable. The report now carries two distinct keys —
`field_findings.stale_price.primary_field_missing_used_bool_fallback` (absent/
None) and `…primary_field_non_numeric_used_bool_fallback` (present but
non-numeric) — each with its own `schema_findings` line. The existing
absent/None finding is preserved unchanged. Pinning tests:
`test_stale_primary_non_numeric_uses_bool_fallback_and_surfaced`,
`test_stale_absent_none_fallback_still_works_and_distinct`,
`test_stale_numeric_primary_no_fallback_findings`.

**B. [IMPORTANT — top-N slice evaluability vs global evaluability].**
Round 2 nulled `topN_removed`/`topN_removed_rate` only when the gate was
GLOBALLY not_evaluable. If the first `top_n` rows were all unevaluable for a
gate but a later (beyond-top-N) row made the gate globally evaluable, the
top-N output reported `0`/`0.0` — falsely reading "nothing removed in the top
slice" when the truth was "we could not look at the top slice." Fix: the
null decision now keys on the evaluability of the TOP-N SLICE specifically
(`topn_slice_evaluable = count of first top_n rows with a non-None verdict`);
when that is zero, both `topN_removed` and `topN_removed_rate` are `null`
regardless of global evaluability. Evaluable top-N slices keep numeric values
(rate denominator unchanged: `min(top_n, total_rows)`). Pinning tests:
`test_topn_slice_unevaluable_nulls_topn_even_if_globally_evaluable`,
`test_topn_slice_partially_evaluable_keeps_numeric`.

Local suite after round 3: **59 tests pass** (`pytest -q`). `black` clean.
