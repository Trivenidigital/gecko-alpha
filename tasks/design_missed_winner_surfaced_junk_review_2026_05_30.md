**New primitives introduced:** NONE — this design reuses the established offline-audit primitive shape already shipped in `scripts/audit_clean_price_path.py`, `scripts/audit_price_path_coverage.py`, and `scripts/audit_signal_early_usefulness.py`: pure `build_report(conn, now)`, read-only `file:{db}?mode=ro` sqlite via `_open_ro`, argparse `main()` returning exit 0/2, a `check_schema_precondition(conn)` → `SchemaError` gate, a top-level `sqlite3.Error` handler that emits `{"stage":"query"}` and exits 2, UTC-`Z` timestamp parsing (`_parse_iso_z`), and `N_FLOOR` n-gating. The only net-new artifacts are one offline script `scripts/audit_missed_winner_surfaced_junk_review.py` and its test module. It introduces NO new tables, NO new columns, NO new config keys, NO writes, and NO live-ranking pathway.

## Hermes-first analysis

Checked the Hermes skill hub (`hermes-agent.nousresearch.com/docs/skills`) + awesome-hermes-agent ecosystem for capabilities covering this offline retrospective.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Retrospective trade attribution / missed-opportunity analysis | none found — Hermes skills cover live agentic execution, browsing, messaging, on-chain swaps; none perform offline post-hoc SQLite cohort attribution over a project-private schema | build from scratch (offline project-specific sqlite attribution over gecko-alpha's own `candidates` / `gainers_comparisons` / `volume_history_cg` / `paper_trades` tables) |
| Price-path winner/junk classification | none found (gecko-alpha-internal) — but it exists IN-TREE | reuse in-tree `scripts/audit_clean_price_path.py` classifier (see §2 Composition decision) |
| Candidate-filter what-if / gate replay | none found — Hermes has no offline gate-replay primitive | build from scratch (replays gecko-alpha's own focus/candidate gate over historical persisted rows) |
| Report rendering / structured findings emission | none applicable — the sibling scripts already establish the JSON/text findings convention | reuse sibling convention; no Hermes dependency |

**awesome-hermes-agent ecosystem check + verdict:** the awesome-hermes-agent ecosystem is oriented to live agent tooling (wallet/swap adapters, social listeners, browser drivers, messaging bridges); there is no offline retrospective-analytics or SQLite-attribution skill that fits an air-gapped read-only audit over a project-private schema. Verdict: build from scratch, composing the in-tree price-path classifier; no Hermes dependency is warranted.

---

# Design: BL-NEW-MISSED-WINNER-SURFACED-JUNK-REVIEW

**Date:** 2026-05-30
**Status:** DESIGN ONLY — no script, no tests are produced by this doc.
**Worktree/branch:** `feat/missed-winner-surfaced-junk-review` @ `C:\projects\gecko-alpha-wt\missed-winner` (off `origin/master` tip `c74a23eb`, "signal early-usefulness scorecard #328").
**Artifact to be built later:** `scripts/audit_missed_winner_surfaced_junk_review.py` + `tests/test_audit_missed_winner_surfaced_junk_review.py`.

## Canonical spec (verbatim from `backlog.md` lines 200-212)

> **Provenance note:** the full canonical block (backlog.md lines 200-212) was read verbatim via the Read/Grep tools and is reproduced below; no spec-text reconciliation gap remains. (`backlog.md` is plain UTF-8 — an earlier in-session "UTF-16" guess was wrong.) Every schema fact below is cited file:line against `scout/db.py` and the merged sibling scripts.

> ### BL-NEW-MISSED-WINNER-SURFACED-JUNK-REVIEW: offline review loop for signal quality
> **Status:** PROPOSED / OFFLINE-ONLY 2026-05-28.
> **Tag:** `todays-focus` `signal-quality` `offline-review` `lookahead-guard`
> **Goal:** Produce a daily offline report answering: which winners were surfaced early, which winners were hidden and why, which surfaced rows were junk after 1h/4h, and which factual filters would have removed junk without hiding winners.
> **Why:** The system needs a disciplined feedback loop for signal quality. The loop should improve understanding first, then justify narrowly scoped filter changes only when data supports them.
>
> **Required output sections:**
> - Surfaced winners: rows in Today's Focus that later met the pinned movement threshold.
> - Hidden winners: rows absent from Today's Focus, with terminating lever such as stale price, no venue route, block cause, corpus mismatch, or filter exclusion.
> - Surfaced junk: rows shown that had no favorable move and/or immediate adverse move within pinned windows.
> - Candidate filter simulation: factual filters and their junk-removal / winner-retention counts.
>
> **Hard anti-scope:** no future-runner labels in live ranking, no automatic threshold tuning, no auto-disabling signals, no alerts, no sizing, no execution. Any live filter change requires a separate PR with pre-registered thresholds and no-lookahead proof.

Supporting context from the adjacent operator entries that this design honors:
- Movement windows are **1h / 4h / 24h** (the scorecard entry at backlog.md:163 — "Max favorable move within 1h, 4h, and 24h after detection" — and the spec's own "junk after 1h/4h"). These are the operator's **pinned windows**.
- The sibling `BL-NEW-CLEAN-PRICE-PATH-AUDIT` (backlog.md:170-185, shipped as `scripts/audit_clean_price_path.py`) defines the winner/junk path buckets and the guardrails: "Require temporal ordering: runner event after detection/surface timestamp," "Require lookback maturity for all cohorts," "Keep output offline; no live ranking or curation changes without a follow-up design."
- backlog.md:174 (operator, verbatim): "A token that stops, languishes, and runs weeks later is a different opportunity… not hindsight-only identity matches." This is the operator stating the very limitation §3-L1 documents — weeks-later runups are NOT missed winners to chase. The design and the 7-day-retention limitation point the same direction.

---

## 1. Purpose & scope

A daily, **offline-only** report with the four operator-required sections, in spec order:

1. **Surfaced winners** — rows in Today's Focus that later met the pinned movement threshold (good early calls).
2. **Hidden winners + terminating lever** — rows absent from Today's Focus that nonetheless ran, each annotated with the persisted lever that explains the absence (stale price, no venue route, block cause, corpus mismatch, filter exclusion) — to the extent persisted.
3. **Surfaced junk** — rows shown (Today's Focus) with no favorable move and/or an immediate adverse move within the pinned 1h/4h windows.
4. **Candidate filter simulation** — factual filters replayed over historical candidate rows, each reported with junk-removal count and winner-retention count.

**Hard guardrail (enforced in code, not just prose):** decision-support for the human operator ONLY. MUST NOT feed live ranking, scoring, ordering, labelling, threshold tuning, signal enable/disable, alerts, sizing, or execution. Every label here uses post-detection future price (lookahead), illegal for live ranking; wiring any of it live requires a **separate PR with pre-registered thresholds and a no-lookahead proof** (spec line 212).

---

## 2. Composition decision — reuse vs replicate the price-path classifier

`scripts/audit_clean_price_path.py` (merged on master) is the canonical post-detection winner/junk classifier. Its buckets are `continuous_move | drawdown_then_recovery | unrelated_later_move | no_runner`, with `RUNNER_THRESHOLD_PCT=50.0`, `CONTINUOUS_MAX_DRAWDOWN_PCT=25.0`, `MATURITY_HOURS=24.0`, `N_FLOOR=10` (verified at file lines 29-34, 43-51).

- **Decision: IMPORT the classifier, do not copy its thresholds.** The new script imports `audit_clean_price_path` and calls its pure per-token classifier `_classify_one(candidate, conn, *, window_hours, run_threshold, drawdown_threshold, flat_gap_hours, flat_band_pct, min_points, maturity_hours, now)` (verified at `scripts/audit_clean_price_path.py:208-313`), which returns `{"coin_id","cohort_source","bucket",...}` where `bucket ∈ {continuous_move, drawdown_then_recovery, unrelated_later_move, no_significant_move, insufficient_data, window_incomplete}`. This keeps both audits aligned on what "ran"/"continuous"/"unrelated" mean. The constants to pass through are the sibling's CLI defaults: `run_threshold=30.0`, `drawdown_threshold=15.0`, `flat_gap_hours=48.0`, `flat_band_pct=10.0`, `min_points=5`, `window_hours=168`, `maturity_hours=window_hours` (verified at `audit_clean_price_path.py:761-812`).
- **Import mechanism (RESOLVED — A1):** `scripts/` is not a package, so use the path-based `importlib` load the sibling tests use: `importlib.util.spec_from_file_location(...)` against the absolute path of `audit_clean_price_path.py` next to this script (`Path(__file__).with_name("audit_clean_price_path.py")`). Reviewer confirm the exact spelling against `tests/test_audit_clean_price_path.py`, but the script is import-safe: all logic is in module-level pure functions; the only top-level side effect is guarded by `if __name__ == "__main__": sys.exit(main())` (verified `audit_clean_price_path.py:896-897`). **A1 is therefore CLOSED, not open.**
- **Winner/junk mapping (operator semantics → classifier buckets):**
  - **winner** = `continuous_move` OR `drawdown_then_recovery` (a path a trader could realistically inspect and hold — matches backlog.md:174/177-178).
  - **NOT a missed/surfaced winner** = `unrelated_later_move` (weeks-later catalyst after a flat/stale window — explicitly excluded per backlog.md:174,179) and `no_significant_move`. `insufficient_data` / `window_incomplete` are unclassifiable and excluded from every cohort denominator (mirror the sibling's `METRIC_NULL_BUCKETS`).
  - **junk** = `no_significant_move` AND (no favorable move within 1h/4h OR an immediate adverse move within the pinned windows) — see §6.1. Junk uses the pinned-window favorable/adverse test directly (spec line 209), which is a finer-grained criterion than the classifier's runner bucketing; the new script computes per-window favorable/adverse extrema itself (the sibling computes a single-window MFE/MAE, not per-1h/4h, so the new script adds the per-window extrema while reusing the classifier for the runner bucket).
- **A2 — classifier surface: RESOLVED.** `_classify_one` is a pure callable taking `conn` + a candidate dict (`coin_id`/`detection_ts`/`detected_price`/`cohort_source`) + `now`; no extraction PR is needed. The new script builds candidate dicts the same shape `_build_cohort` does (`audit_clean_price_path.py:639-675`) and calls `_classify_one` per token.

---

## 3. SCOPE-LIMITATION block (PROMINENT — read before trusting any number)

Structural observability limits of gecko-alpha's persisted schema. NOT bugs to fix here; they bound what the report can honestly claim. The report MUST print L1-L5 in every run.

- **L1 — `volume_history_cg` has ~7-day retention.** Post-detection price path is observable for only ~7 days after a snapshot is written. **Consequence: "winners that ran weeks later" are UNOBSERVABLE** — the price path is gone before the late runup. This is the SAME limitation `audit_clean_price_path.py` / `audit_price_path_coverage.py` already operate under, and the operator EXPLICITLY agrees these are "a different opportunity… not hindsight-only identity matches" (backlog.md:174). The report is therefore CORRECT to mark such cases out of scope. **Note: L1 is a data-availability ceiling on how far past detection we can observe; it is distinct from the pinned 1h/4h/24h *movement* windows (§6), which fit comfortably inside the 7-day window for any candidate matured ≥24h.**
- **L2 — "Surfaced" = membership in Today's Focus, which has NO persisted membership table.** Today's Focus is recomputed live and persisted only client-side (localStorage). **The only persisted proxy for "was surfaced" is `gainers_comparisons.appeared_on_gainers_at`, which covers the gainers cohort ONLY.** Rows surfaced via other Today's-Focus lanes (scorer, trending, chain dispatch) have NO persisted surface record. Therefore §1/§3 "surfaced" is observable ONLY for the gainers cohort; the report MUST state this and MUST NOT claim coverage of the full Today's-Focus set. **V1 CONFIRMED against `scout/db.py`:** `gainers_comparisons` (db.py:925-947) has `coin_id TEXT`, `appeared_on_gainers_at TEXT NOT NULL` (db.py:931), `detected_price`, `peak_price`, `peak_gain_pct REAL` (added via migration db.py:1249-1257). The same table is already consumed by both sibling audits' gainers cohort path, so this is a proven, not assumed, data source.
- **L3 — The "terminating lever" for non-surfacing is largely NOT persisted per-candidate.** The operator's enumerated levers are: stale price, no venue route, block cause, corpus mismatch, filter exclusion (spec line 208). Of these, only some leave a persisted row:
  - *stale price* — derivable IF a per-candidate price-freshness timestamp is persisted (price_cache age); known to exist for held positions (memory: open-position price_cache staleness) but per-candidate freshness at detection is **MUST-VERIFY-AT-BUILD (V2)**.
  - *corpus mismatch* — derivable: the two-corpus split (scorer $10K-$500K vs CG-markets-watcher $10K-$500M) is structural (memory: two-corpus architecture; filter at coingecko.py:119). A candidate's mcap vs corpus bounds is checkable IF mcap-at-detection is persisted (**V3**).
  - *filter exclusion / block cause* — derivable ONLY if a `block_reason` (or equivalent) column is persisted on `candidates`; **ingest-stage mcap filters reject tokens that frequently never get a `candidates` row at all**, so the lever is applied in-flight and discarded (memory: POD plumbing-gap — ingest-entry filter is the actual gate, invisible downstream). **MUST-VERIFY-AT-BUILD (V4): does `candidates` carry a `block_reason`/exclusion column?**
  - *no venue route* — derivable IF venue/tradability facts are persisted per candidate (**V5**).
  Levers that are NOT persisted are reported as `lever="unobserved"` with an explicit count — never guessed.
- **L4 — `paper_trades` is a dispatch cohort, not a surface cohort.** Paper trades exist only for tokens that passed the dispatch gate; they give high-fidelity `peak_pct`/`checkpoint_*` for THAT subset but are disjoint from the gainers surface proxy. Do not conflate "paper-traded" with "surfaced in Today's Focus."
- **L5 — `candidates.contract_address` holds the CG slug for `chain='coingecko'` rows** (memory: feedback_cg_slug_not_address). Cross-source joins on contract_address are source-unsafe. **The price path joins on `volume_history_cg.coin_id` (db.py:863), NOT contract_address** — the siblings use `coin_id` as the join key throughout (`_price_series`/`_load_price_path` query `WHERE coin_id = ?`), and the gainers cohort's `coin_id` matches `volume_history_cg.coin_id`. This report keys winner/junk labels per-row on `coin_id` within the gainers cohort and treats cross-source contract_address identity resolution as OUT OF SCOPE.

**Confirmed `volume_history_cg` schema (db.py:863-882):** columns `coin_id`, `price`, `recorded_at` (NOT `snapshot_at`/`price_usd`). Index `(coin_id, recorded_at)`. Writer `scout/spikes/detector.py` prunes rows older than 7 days (the L1 retention ceiling). The schema-precondition (§5.3) keys on these exact names.

**Net honest framing the report header must print:** "This report observes the GAINERS-COMPARISON surface proxy for 'surfaced' (not the full Today's Focus set), classifies movement over the pinned 1h/4h/24h windows for candidates matured ≥24h, and can see post-detection price for only ~7 days. It cannot see non-gainers surfaces, weeks-later runups, or in-flight rejection reasons that were never persisted."

---

## 4. Per-section buildability matrix (verified against the sibling scripts' confirmed schema; flagged where only brief-asserted)

| Section | Backing data | Verdict | Why |
|---|---|---|---|
| §1 Surfaced winners | `gainers_comparisons.appeared_on_gainers_at` (surface proxy, V1) + classifier winner bucket over `volume_history_cg` within pinned windows | **BUILDABLE (gainers cohort only)** | surface proxy + post-detection winner path both observable for the gainers cohort |
| §1 full Today's-Focus universe | localStorage-only membership (L2) | **RESCOPED → gainers cohort** | no persisted membership for scorer/trending/chain surfaces |
| §2 Hidden winners (≤7d, gainers-surface-negative) | classifier winner bucket over `volume_history_cg` for tokens with NO `appeared_on_gainers_at` | **BUILDABLE (≤7d, partial)** | "ran but not on the gainers surface" is observable |
| §2 Hidden winners (weeks-later) | none (L1) | **NOT-BUILDABLE-OFFLINE** | 7-day retention erases the path; operator agrees these are a different opportunity (backlog.md:174) |
| §2 Terminating lever — corpus mismatch | mcap-at-detection vs corpus bounds (V3) | **BUILDABLE (if V3)** | two-corpus split is structural and checkable from persisted mcap |
| §2 Terminating lever — stale price / no venue route | per-candidate freshness / venue facts (V2, V5) | **BUILDABLE IF PERSISTED; else RESCOPED to `unobserved`** | depends on whether detection-time freshness/venue is written per candidate |
| §2 Terminating lever — block cause / filter exclusion | `candidates.block_reason` (V4) | **BUILDABLE IF PERSISTED; else NOT-BUILDABLE-OFFLINE** | ingest-stage rejects often have no `candidates` row at all (POD plumbing-gap); lever discarded in-flight |
| §3 Surfaced junk | `gainers_comparisons.appeared_on_gainers_at` + pinned-window favorable/adverse test over `volume_history_cg` | **BUILDABLE (gainers cohort only)** | mirror of §1 with the junk criterion (spec line 209) |
| §4 Candidate filter simulation | `candidates` rows + persisted fields each factual filter reads + classifier outcome labels | **BUILDABLE for filters whose inputs are persisted; per-filter RESCOPED otherwise** | a factual filter is replayable only if its inputs (mcap, liquidity, freshness, venue) are persisted on `candidates` |

**MUST-VERIFY-AT-BUILD summary:** V1 (`gainers_comparisons.appeared_on_gainers_at`,`peak_gain_pct`), V2 (per-candidate price freshness), V3 (mcap-at-detection), V4 (`candidates.block_reason`/exclusion), V5 (venue/tradability facts). The schema-precondition gate (§5.3) keys ONLY on the columns each ENABLED section needs; a section whose backing column is absent is omitted with an explicit `rescoped_reason`, NOT silently emptied.

---

## 5. CLI, contract, and exit semantics (mirror siblings exactly)

### 5.1 argparse (extends the sibling CLI shape: `--db` required, `--now`, `--json`)
```
audit_missed_winner_surfaced_junk_review.py
  --db PATH            (required) path to scout.db; opened read-only via file:{PATH}?mode=ro
  --now ISO8601Z       (optional) inject 'now' for deterministic runs/tests; default datetime.now(timezone.utc)
  --json               (optional) emit JSON; default human text. Identical content both forms.
  --n-floor INT        (optional, default 10) min cohort size before a section emits a verdict (mirror N_FLOOR)
  --maturity-hours FLOAT (optional, default 24.0) candidate must be ≥ this old to be classified (mirror MATURITY_HOURS)
```
Pinned-window constants (NOT CLI flags, to keep them pinned-before-audit per guardrail): `WINDOWS_HOURS = (1.0, 4.0, 24.0)` (matches the sibling scorecard and spec). All times normalized to UTC `Z`.

### 5.2 Exit codes (verbatim pattern from `audit_signal_early_usefulness.py` lines 98-119 — the shape that cleared Codex)
- `exit 0` — report produced (sections may be n-gated to null or rescoped; both are success).
- `exit 2` on **bad `--now`** — `ValueError` from `_parse_iso_z` → message to stderr → return 2.
- `exit 2` on **cannot open DB read-only** — `sqlite3.Error` from `_open_ro` → return 2.
- `exit 2, stage="schema"` — `SchemaError` from `check_schema_precondition` → `print(json.dumps({"stage":"schema","error":...}), file=sys.stderr)` → return 2. Never degrade to empty results.
- `exit 2, stage="query"` — any `sqlite3.Error` raised during `build_report` → `print(json.dumps({"stage":"query","error":...}), file=sys.stderr)` → return 2. **Never swallow a sqlite error into empty/zero output** (both siblings were blocked by Codex for exactly this — design it correct from the start).
The two exit-2 stages are distinct and independently tested.

### 5.3 Schema precondition (mirror the siblings' `_schema_precondition_error` + `PRAGMA table_info` exactly)
Reuse the verbatim sibling pattern (`audit_signal_early_usefulness.py:787-808` `_schema_precondition_error` + `_table_exists`/`_column_exists`): a `sqlite_master` table-existence check, then `PRAGMA table_info(<t>)` per table asserting required columns; return a stage="schema" string (→ exit 2) on any miss. Baseline required (CONFIRMED present against `scout/db.py`):
- `volume_history_cg`: `coin_id`, `price`, `recorded_at` (db.py:863 — exact names; the price-path source for every winner/junk label).
- `gainers_comparisons`: `coin_id`, `appeared_on_gainers_at`, `peak_gain_pct` (db.py:925-947 — the ONLY persisted surface proxy; §1 and §3 are meaningless without it, so it is BASELINE, resolving A3 in favor of hard-fail).
Section-conditional (probe via schema_findings, NOT baseline): `candidates` table (db.py:521) for §2/§4 — present, but its per-candidate lever columns (V2-V5) are conditional; lever columns for §2; filter-input columns for §4. If a baseline table/column is missing → `exit 2 stage="schema"`. If a section-conditional column is missing → that section/lever/filter is RESCOPED with `rescoped_reason` (still `exit 0`), so buildable sections run even when one lever isn't persisted — mirroring the sibling's OPTIONAL-cohort degrade-via-schema_findings pattern (`audit_signal_early_usefulness.py:55-58`).
- **A3 RESOLVED:** `gainers_comparisons` is BASELINE (hard `exit 2 stage="schema"` if absent) because two of four sections are meaningless without it; `candidates` lever columns (V2-V5) and §4 filter inputs are section-conditional (rescope, not exit-2).

### 5.4 OFFLINE / no-write / no-lookahead-into-live contract (ENFORCEABLE)
Encode the spec's hard anti-scope (line 212) as a runtime contract (memory: anti_scope_as_runtime_contract):
1. **Read-only DB** — `_open_ro` uses `file:{db}?mode=ro` (verbatim from siblings, line 117-122 / 75-79). Test asserts an attempted write raises `sqlite3.OperationalError: attempt to write a readonly database`.
2. **No filesystem writes** beyond stdout — `build_report` returns a dict; `main()` only prints. Test asserts no file is created in `tmp_path` other than the injected DB.
3. **Output-key allow-list (anti-lookahead-into-live-ranking)** — `build_report` returns ONLY allow-listed top-level keys; the allow-list REJECTS any key whose name contains (case-insensitive) `rank`, `score`, `order`, `priority`, `weight`, `enable`, `disable`, `alert`, `size`, `execute`, `tune`, or `threshold_change` — i.e. the exact live-levers the spec forbids (line 212: no live ranking / threshold tuning / disabling signals / alerts / sizing / execution). Allowed top-level keys exactly: `generated_at`, `pinned_windows_hours`, `surface_cohort`, `scope_caveats`, `surfaced_winners`, `hidden_winners`, `surfaced_junk`, `candidate_filter_simulation`, `schema_findings`, `rescoped_sections`, `counts`. A test enumerates returned keys (recursively at the levels checked) and asserts none match the banned substrings and every top-level key is on the allow-list.
   - **AMBIGUITY A4:** banned substrings would also reject legitimate descriptive backing fields (`peak_gain_pct` is fine; a `gainers_rank` column would be rejected). Policy: rename any banned-substring backing column to a neutral report key (`gainers_position` not `gainers_rank`) so the guard stays a hard wall. Reviewer confirm. NOTE the filter-simulation's "winner-retention / junk-removal counts" are COUNTS, not scores/ranks — name them `winner_retained_count` / `junk_removed_count` (no banned substring).
4. **No-lookahead docstring** — module docstring states every winner/junk/lever label is post-detection (lookahead-tainted) and illegal for live ranking, pointing to spec line 212's "separate PR with pre-registered thresholds and no-lookahead proof." The allow-list (#3) is the mechanical enforcement.

---

## 6. Pure core — signatures + per-section pseudocode

All analysis is pure: inject `conn` + `now` (UTC); no `datetime.now()` inside pure functions (mirror siblings → enables FIXED_NOW tests).

```python
def build_report(conn, now, *, n_floor=10, maturity_hours=24.0) -> dict:
    problems = check_schema_precondition(conn)   # baseline tables; raises SchemaError -> exit 2 stage=schema
    if problems: raise SchemaError("; ".join(problems))
    enabled, rescoped = resolve_section_enablement(conn)   # section-conditional column probe (§5.3)
    return {
        "generated_at": iso_z(now),
        "pinned_windows_hours": [1.0, 4.0, 24.0],
        "surface_cohort": "gainers_comparisons",            # honest: NOT full Today's Focus (L2)
        "scope_caveats": SCOPE_CAVEATS,                      # L1..L5, always present
        "surfaced_winners": section_surfaced_winners(conn, now, maturity_hours, n_floor) if enabled.s1 else rescoped.s1,
        "hidden_winners":   section_hidden_winners(conn, now, maturity_hours, n_floor)   if enabled.s2 else rescoped.s2,
        "surfaced_junk":    section_surfaced_junk(conn, now, maturity_hours, n_floor)    if enabled.s3 else rescoped.s3,
        "candidate_filter_simulation": section_filter_sim(conn, now, maturity_hours, n_floor),
        "schema_findings": problems,            # [] when clean
        "rescoped_sections": rescoped.reasons,  # explicit per-section rescope notes
        "counts": {...},                        # observed n per section, for n-gate transparency
    }
```

### 6.1 §1 / §3 — surfaced winners & junk
```
surfaced = SELECT gc.coin_key, gc.appeared_on_gainers_at, c.first_seen_at
           FROM gainers_comparisons gc JOIN candidates c ON <coin key>
           WHERE gc.appeared_on_gainers_at IS NOT NULL
             AND c.first_seen_at <= now - maturity_hours      # lookback maturity (guardrail)
for each surfaced token:
    path = price_path.classify(conn, coin_id, detection_ts=first_seen_at, now)   # REUSE clean-price-path
    fav, adv = window_extrema(conn, coin_id, first_seen_at, windows=(1h,4h,24h)) # max favorable / max adverse per window
    if path.bucket in ("continuous_move","drawdown_then_recovery"):  -> §1 winner
    elif (no favorable move in 1h/4h) or (immediate adverse move within pinned windows): -> §3 junk
    # unrelated_later_move / ambiguous -> neither (do not count as surfaced winner; backlog.md:179)
n-gate each list at n_floor: if n < floor -> {"verdict": None, "n": n, "reason":"INSUFFICIENT_DATA"}; raw n always shown
```

### 6.2 §2 — hidden winners + terminating lever
```
winners_observed = tokens whose price_path.classify == continuous_move|drawdown_then_recovery within ≤7d, matured ≥24h
for each winner_observed:
    surfaced = (token has gainers_comparisons.appeared_on_gainers_at NOT NULL)
    if surfaced: continue        # that's a §1 hit, not hidden
    lever = attribute_terminating_lever(conn, token)   # OBSERVABLE levers only (L3):
    #   "corpus_mismatch"   if mcap-at-detection outside the candidate's corpus bounds (V3)
    #   "stale_price"       if per-candidate price freshness at detection exceeds staleness bound (V2)
    #   "no_venue_route"    if persisted venue/tradability facts show no route (V5)
    #   "block_cause"       if candidates.block_reason present (V4)
    #   "no_candidate_row"  winner exists in volume_history_cg but has NO candidates row (ingest-stage drop; specific filter UNOBSERVED — POD plumbing-gap)
    #   "unobserved"        none of the above resolvable
    record(token, lever)
emit lever_breakdown with explicit counts INCLUDING unobserved + no_candidate_row;
caveat: ingest mcap filters / in-flight rejects are NOT persisted (L3) so the exact filter for those is not recoverable offline
n-gate the section at n_floor
```

### 6.3 §4 — candidate filter simulation
```
# Each FACTUAL filter is a pure predicate over PERSISTED candidate columns (no localStorage/runtime state).
FILTERS = [ ("mcap_in_corpus_bounds", needs mcap col),
            ("price_fresh_at_detection", needs freshness col),
            ("has_venue_route", needs venue col),
            ("liquidity_above_floor", needs liquidity col), ... ]   # only those whose inputs are persisted are simulated
matured = candidates matured >= maturity_hours
for each filter f whose inputs are persisted:
    kept   = [c for c in matured if f(c)]
    removed= [c for c in matured if not f(c)]
    # classify each via clean-price-path (lookahead — offline only):
    junk_removed_count    = count(removed where label==junk)        # removed junk = good
    winner_removed_count  = count(removed where label==winner)      # removed winner = BAD (hides winners)
    winner_retained_count = count(kept where label==winner)
emit per-filter {filter, junk_removed_count, winner_removed_count, winner_retained_count, n}; n-gate at n_floor
# A filter whose inputs are NOT persisted is listed in rescoped_sections with reason, never silently dropped.
```
- **AMBIGUITY A5 — focus-freshness gate source:** the spec's "factual filters" overlap the live Today's-Focus / focus-freshness gate. Locate that gate's source (`scout/` or `dashboard/` Today's-Focus recompute path) and confirm it is a pure function of PERSISTED candidate columns + `now`. If it reads non-persisted/localStorage state, §4 simulates only the persisted-input subset and lists the rest as rescoped. Reviewer to confirm the canonical filter list with the operator (the spec says "factual filters" without enumerating them).
- **No-lookahead-into-live restated:** the "would this filter have kept winners / removed junk" evaluation is lookahead-tainted; legal offline, ILLEGAL live. The allow-list (§5.4 #3) keeps these counts out of any ranking-shaped output and the count field names avoid banned substrings.

### 6.4 n-gate discipline
Every verdict-bearing list/filter n-gates at `n_floor` (default 10, matching sibling `N_FLOOR`): below floor → `verdict=None`, `reason="INSUFFICIENT_DATA"`, raw `n` still shown (memory: n_gate_verdicts_against_dashboard_noise). INSUFFICIENT_DATA is shown explicitly, never silently blanked.

---

## 7. TDD test plan (`tests/test_audit_missed_winner_surfaced_junk_review.py`)

Mirror sibling tests: `importlib`-load the script by file path; build a `tmp_path` sqlite with the exact required schema; inject a FIXED_NOW.

1. **§1 surfaced winners** — seed gainers-surface rows + continuous_move / drawdown_then_recovery price paths; assert §1 membership; assert an `unrelated_later_move` surfaced token is NOT counted as a winner.
2. **§3 surfaced junk** — seed surfaced rows with no favorable 1h/4h move and with an immediate adverse move; assert §3 membership; assert winners excluded.
3. **§2 hidden winners** — seed a winner-path token WITHOUT the gainers surface flag; assert it appears in §2; seed a surfaced winner and assert it does NOT (it's §1).
4. **§2 lever attribution** — seed each lever case (`corpus_mismatch`, `stale_price`, `no_venue_route`, `block_cause`, `no_candidate_row`, `unobserved`); assert each maps correctly and that `unobserved` + `no_candidate_row` counts are surfaced explicitly.
5. **§4 filter simulation** — seed candidates a factual filter would keep/remove; assert `junk_removed_count` / `winner_removed_count` / `winner_retained_count`; assert a filter with non-persisted inputs is listed in `rescoped_sections`, not silently dropped.
6. **Schema-precondition exit-2** — drop a baseline table → `exit 2 stage="schema"` + missing-identifier finding; drop a baseline column (table present) → same. One test per baseline table + one representative missing column.
7. **Section rescope (not exit-2)** — present baseline tables but absent §2 lever column / §4 filter-input column → `exit 0` with that section/filter in `rescoped_sections` and a reason; assert other sections still compute.
8. **Query-error exit-2** — induce a `sqlite3.Error` mid-query (e.g. a malformed view shadowing a required table) → `exit 2 stage="query"`; assert NOT swallowed into empty results.
9. **Read-only enforcement** — assert an attempted write via the script's connection raises the readonly error; assert `build_report` performs no writes.
10. **Anti-scope output allow-list** — assert `build_report` returns ONLY allow-listed top-level keys; assert NO returned key contains banned substrings (`rank/score/order/priority/weight/enable/disable/alert/size/execute/tune/threshold_change`); include a regression where a backing column is `gainers_rank` and assert it surfaces as `gainers_position` (or is omitted) — never as a banned key; assert filter counts are named `*_count`.
11. **Lookahead guard** — assert the module docstring states no-lookahead-into-live and references the separate-PR requirement; assert no section emits a live-ranking-shaped artifact (covered mechanically by #10).
12. **n-gate** — n < floor → `verdict is None` + `reason="INSUFFICIENT_DATA"` + raw n; n ≥ floor → real verdict.
13. **maturity gate** — a candidate younger than `maturity_hours` is excluded from all sections (temporal-ordering/lookback-maturity guardrail).
14. **temporal ordering** — a "runner" whose move precedes detection is excluded (runner event must be strictly after detection — backlog.md:183).
15. **FIXED_NOW determinism** — same DB + same `--now` → byte-identical report (UTC-Z stable).
16. **scope_caveats always present** — L1..L5 present in every report regardless of section emptiness.
17. **bad `--now` exit-2** — malformed `--now` → exit 2 (mirror sibling).

---

## 8. Open design decisions / ambiguities for reviewer judgment

- **A1 — CLOSED.** Path-based `importlib` against `Path(__file__).with_name("audit_clean_price_path.py")`; script is import-safe (`__main__`-guarded). Reviewer to confirm exact spelling matches `tests/test_audit_clean_price_path.py`.
- **A2 — CLOSED.** `_classify_one` (audit_clean_price_path.py:208-313) is a pure per-token callable; no extraction PR needed.
- **A3 — CLOSED.** `gainers_comparisons` is BASELINE schema (hard exit-2 if absent); `candidates` lever columns + §4 filter inputs are section-conditional (rescope).
- **A4 — OPEN (reviewer).** Confirm the rename-to-neutral-key policy for banned-substring backing columns; confirm filter counts named `*_count` pass the allow-list. (`peak_gain_pct` contains no banned substring; `gainers_rank` would — but no `*_rank` column exists on `gainers_comparisons`, so this is precautionary.)
- **A5 — OPEN (reviewer + operator).** Enumerate the canonical "factual filters" for §4 with the operator (spec line 210 says "factual filters" without listing them). Candidate inputs to probe on `candidates`/snapshots: mcap-at-detection, liquidity, price freshness, venue route. The live focus-freshness recompute lives in the Today's-Focus path (`dashboard/db.py` get_todays_focus + `scout/gainers/tracker.py`); confirm whichever filter set the operator wants is a pure function of PERSISTED columns. Filters whose inputs aren't persisted are rescoped.
- **A6 — OPEN (operator).** Confirm the "pinned movement threshold" for §1 winners. This design maps it to the sibling classifier's `run_threshold=30.0` default + winner buckets; the operator may intend a different favorable-move threshold per 1h/4h/24h window. Pin before build (guardrail: "Pin runner definition before the audit").
- **A7 — OPEN (reviewer).** Confirm `n_floor` default for the daily gainers-surface cohort (siblings use `min_n=5` for cohort presence and `min_n_dist=10` for distributions). Recommendation: adopt the sibling's two-tier gate (`--min-n 5` cohort floor, `--min-n-dist 10` verdict/distribution floor) rather than a single `--n-floor`, for parity. Reviewer to confirm.
- **V2-V5 — MUST-VERIFY-AT-BUILD (remaining):** V2 per-candidate price freshness at detection (held-position freshness exists; per-candidate-at-detection unconfirmed — likely via `paper_trade_entry_snapshots` for the dispatch cohort only); V3 mcap-at-detection (two-corpus split is structural; confirm mcap is persisted per candidate); V4 `candidates.block_reason`/exclusion — **NO `block_reason` column was observed on `candidates` (db.py:521); L3 stands: block-cause lever is largely NOT persisted, report as `unobserved`/`no_candidate_row`**; V5 venue/tradability facts (the scorecard sibling notes "no venue column on paper_trades" — venue_* tables are the BL-055 live layer keyed by (venue,symbol), so no-venue-route lever is likely `unobserved` for the gainers cohort). V1 is CONFIRMED (see §3-L2). Each ENABLED section's schema-precondition keys on its confirmed columns and fails closed (schema, baseline) or rescopes (section-conditional) if absent.

---

## 9. Summary buildability verdict

- §1 surfaced winners — **BUILDABLE** (gainers-surface cohort; pinned windows; matured ≥24h).
- §2 hidden winners — **BUILDABLE (≤7d, partial)**; weeks-later runups **NOT-BUILDABLE-OFFLINE** (L1, and operator-agreed out of scope, backlog.md:174); terminating levers BUILDABLE per-lever where persisted (corpus_mismatch via V3 most likely), the rest reported as `unobserved`/`no_candidate_row` with explicit counts (L3).
- §3 surfaced junk — **BUILDABLE** (gainers cohort; pinned 1h/4h favorable/adverse test).
- §4 candidate filter simulation — **BUILDABLE per-filter** for filters whose inputs are persisted on `candidates`; filters with non-persisted inputs are RESCOPED with explicit reason (A5).

The report is honest-by-construction: prints L1-L5 every run, n-gates every verdict, marks rescoped sections explicitly, and mechanically refuses to emit any live-ranking / threshold-tuning / alert / sizing / execution-shaped key (spec line 212 enforced as a code-level allow-list).
