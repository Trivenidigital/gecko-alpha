**New primitives introduced:**
- `scripts/check_live_candidates_contract.py` — runtime contract validator for `/api/live_candidates` (CLI + JSON output)
- `tests/test_check_live_candidates_contract.py` — unit tests exercising the validator against fixture-shaped payloads (golden, banned-language, schema-drift, counter_flags heterogeneity)

# Plan: BL-NEW-LIVE-CANDIDATES-CONTRACT-SMOKE

Date: 2026-05-23.

## Goal

Close the post-PR-#229 gap: response_model validation passed in unit tests yet 500'd in prod against real `predictions.counter_flags` shape. Build a deterministic contract+smoke validator the operator can run locally OR against srilu to certify the `/api/live_candidates` response shape **before** building the frontend cockpit panel on top of it.

This is the "make the API reliable enough to build UI on" deliverable from the operator's P1.

V1 is **runtime-evidence-only**: the script makes 1 HTTP call against a configurable URL, validates the response against a frozen contract, and exits 0/non-zero. No DB writes, no service restart, no external calls beyond the target URL.

## Drift-check (in-tree)

- `grep "live_candidates" scripts/` → no existing scripts.
- `grep "check_.*_contract" scripts/` → no existing contract-test scripts.
- Existing `scripts/check_*.py` (e.g. `check_source_calls_lag.py`, `check_chain_anchor_health.py`) follow a `argparse → query → assert → exit-code` style; new script will mirror that pattern.
- No frontend panel exists yet — this validator is the gate that decides when one is safe to build.

Result: clean drift-check. Building net-new is justified.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| HTTP contract / smoke testing for internal APIs | none on public Skills Hub (`hermes-agent.nousresearch.com/docs/skills/`); these are test-runner concerns, not agent capabilities | Build from scratch as a stdlib-only Python script |
| Deterministic label-safety auditing (banned-token check) | none — Hermes skills are content-generation tools, not response-validation tools | Build from scratch; banned-token list lives in this script as a frozen constant |
| Pydantic-model-vs-prod-data shape verification | none — covered by the existing structural-vector PR review prompt (see `memory/feedback_response_model_vs_prod_data_shape.md`) | This script complements that prompt: the prompt catches at-review-time, the script catches at-deploy-time |
| Price truth / PnL / identity / execution | N/A — script is read-only, no external API calls | Stdlib only (urllib) |

Hermes deployed-surface check is documented in the parent cockpit plan (`tasks/plan_live_decision_cockpit_v1_2026_05_23.md`); separate operator-gated paste-back to formally close that gate. This script doesn't load any Hermes surface.

Awesome-Hermes-agent ecosystem scan: no `contract-test` / `smoke-runner` / `response-validator` project found that would compose here. Verdict: net-new Python script is justified for this residual gap.

## Acceptance criteria (frozen for review)

The validator MUST:

1. Make exactly one HTTP GET to `<base_url>/api/live_candidates?limit=<L>&window_hours=<W>` with defaults `L=20, W=36` (matching production default).
2. Assert HTTP status == 200.
3. Assert response time under `--slo-ms` threshold (default 3000ms; **provisional — operator will set a P95-based threshold from first 10 production samples before promoting to a CI gate**).
4. Parse JSON; assert top-level is an object with `meta` AND `rows` keys. Unknown top-level keys (e.g., a future `warnings` or `cohort_summary`) MUST emit a **WARNING listing the unknown key name**, not a CRITICAL — `LiveCandidateCockpit` is a plain Pydantic `BaseModel` (no `extra="forbid"`), so forward-compat folds are legitimate (per Vector-A I2).
5. Assert `meta.read_only is True`, `meta.not_trade_advice is True`, `meta.experimental is True` — any False or missing flag is **CRITICAL**. **Governance note (per Vector-B I1):** `not_trade_advice` has no promotion path — it stays True forever; the validator treats any False as CRITICAL with no escape hatch. `read_only` and `experimental` can only flip via a signed operator decision recorded in `backlog.md` AND a coordinated update to this validator's constant — until that coordinated change lands here, True is invariant.
6. Assert `meta.generated_at` parses as ISO8601 UTC and is within 60 seconds of local now (drift gate).
7. Assert `meta.rows_returned == len(rows)`.
8. Assert `meta.open_trades_scanned >= 0` (integer).
9. Assert `meta.window_hours` and `meta.limit` echo the request (sanity).
10. **Per-row complete field-coverage matrix** (per Vector-A C1 — every `LiveCandidateResponse` field gets an explicit assertion; the #229-class bug came from an under-validated field, so closing the matrix is the firewall):

    | Field | Type assertion |
    |---|---|
    | `disclaimer` | non-empty `str` AND lowercased matches `re.search(r'\bnot\s+(trading\|investment\|financial)\s+advice\b', ...)` (per Vector-B I2) AND length ≥ 20 |
    | `token_id` | non-empty `str` |
    | `symbol` | `str \| None` |
    | `name` | `str \| None` |
    | `chain` | `str \| None` |
    | `open_trade_ids` | `list[int]` |
    | `recent_trade_ids` | `list[int]` |
    | `surfaces` | `list[str]` |
    | `actionable` | `int \| None` AND if int, value ∈ {0, 1} |
    | `would_be_live` | `int \| None` AND if int, value ∈ {0, 1} |
    | `opened_at` | `str \| None`; if not None, parses as ISO8601 |
    | `entry_price` | `float \| int \| None` |
    | `pct_from_entry` | `float \| int \| None` |
    | `current_price` | `float \| int \| None` |
    | `market_cap` | `float \| int \| None` |
    | `price_change_24h` | `float \| int \| None` |
    | `price_updated_at` | `str \| None`; if not None, parses as ISO8601 |
    | `price_is_stale` | strict `bool` (per Vector-A I4) |
    | `narrative_fit_score` | `int \| None` |
    | `counter_risk_score` | `int \| None` |
    | `counter_flags` | `list`; each item is `dict` OR `str` (per Vector-A N4: schema-free inner-dict in V1 — producer doesn't enforce a `flag` key); any other item-type is CRITICAL (#229 regression) |
    | `latest_chain_match` | `dict \| None` (per Vector-A I5; if dict, schema-free in V1 — flat-scalar guaranteed by producer) |
    | `entry_quality` | `str` ∈ `{fresh_entry, acceptable_pullback, already_faded, already_ran, too_stale, data_insufficient}` — closed set; any other value is **CRITICAL** (future ambitious labels like `high_conviction` must propose new entries via a design doc, not extend silently) |
    | `verdict` | `str` ∈ `{candidate_review, watch, blocked, data_insufficient}` — closed set; **CRITICAL** otherwise |
    | `inclusion_reasons` | `list[str]` |
    | `risk_reasons` | `list[str]` |

    **Unknown row-level keys → WARNING** (per Vector-B I5 — closes the door on silent additions even if regex-name-firewall in AC#14 misses).

11. **Banned-language gate (recursive — per Vector-B C2).** Walk the response recursively. For every string-valued leaf EXCEPT the explicit identifier/enum allowlist `{meta.generated_at, verdict, entry_quality, chain, symbol, name, token_id, opened_at, price_updated_at}`, normalize the text (`.casefold()`, NFKC, collapse runs of whitespace) and assert it does NOT contain any of these tokens:

    **BANNED_IMPERATIVES_V1** (CRITICAL — verbs-with-objects):
    `buy now`, `sell now`, `trade now`, `go long`, `short this`, `enter here`, `entry signal`, `execute trade`, `ape in`, `aping`, `send it`, `dump it`, `take profit`, `lock in profit`, `lock in gains`, `secure profit`, `cut losses`, `stop loss now`, `accumulate`, `load up`, `loading bags`, `bagging`

    **BANNED_HYPE_V1** (CRITICAL — puffery):
    `moon`, `mooning`, `100x`, `10x`, `1000x`, `gem`, `hidden gem`, `alpha leak`, `this is the one`, `do not miss`, `don't sleep`, `last chance`, `huge upside`, `easy money`, `free money`, `bullish af`, `printing`, `winner`, `lambo`, `top pick`, `best buy`, `strong buy`, `must buy`, `floor is in`, `breakout confirmed`, `next leg up`

    Constants are versioned as `BANNED_IMPERATIVES_V1` and `BANNED_HYPE_V1` so additions show in git blame. Existing simpler tokens `dump` (alone) and `pump` are NOT banned — too ambiguous with neutral price-action prose (`"price dumped 20%"`); only `dump it` / `pump` (alone) is permitted because it appears in legitimate retrospective context.

    **Per Vector-B I4: the banned-language gate applies uniformly regardless of `verdict`.** Even a `data_insufficient` row that won't be acted on must not carry advice-tone text — tone-drift under uncertainty is more dangerous than under high-confidence, not less.

12. **`data_insufficient` invariant (per Vector-A C2):** if `verdict == "data_insufficient"`, then at least one of these conditions must hold:
    - any of `risk_reasons` includes: `no_price_snapshot_for_token_id`, `price_timestamp_unparseable`, `opened_at_unparseable`, `entry_price_missing_or_invalid`, `actionable_null_pre_cutover`, `price_is_stale`
    - OR `entry_quality == "too_stale"`
    - OR `entry_quality == "data_insufficient"` (the `_entry_quality(None)` escape, hit when `pct_from_entry` is None due to missing price or entry_price ≤ 0)
    
    Otherwise: **WARNING** (schema drift, not a bug yet — emits to stderr).

13. **`candidate_review` invariant**: if `verdict == "candidate_review"`, then `actionable == 1` AND `would_be_live == 1` AND `entry_quality ∈ {fresh_entry, acceptable_pullback}` — otherwise **CRITICAL** (the safety gate the cockpit hangs on; verified against `db.py:1293-1297`).

14. **Per-source / KOL ranking firewall (per Vector-B C3):** row-level keys MUST NOT match any of these patterns (matched against lowercased key name via `re.fullmatch`):
    - `kol_(rank|score|weight)`
    - `source_(rank|score|weight)`
    - `channel_(rank|score|weight)`
    - `tg_.*_(rank|score|weight)`
    - `x_.*_(rank|score|weight)`
    - `influencer_.*`
    - `recommended` / `top_pick` / `top_n` / `highest_(rank|score)`
    
    Any match is **CRITICAL**. The operator's pinned safety stance is no per-source/KOL ranking until source-call price coverage becomes rankable; this validator is the firewall against a well-intentioned enrichment PR that adds such a field and slips through other checks.

15. **`counter_flags[i].severity` allowlist (per Vector-B I3):** when a `counter_flags` item is a dict and contains a `severity` key, the value MUST be `∈ {high, medium, low, info}` OR None. Any other value is **WARNING** (not CRITICAL — severity is an enrichment field, not a labeled decision). Severity text is also covered by the recursive banned-language scan from AC#11 (because it's a string leaf), so an unsafe `"severity": "MUST BUY NOW"` would still be CRITICAL via AC#11.

The validator MAY emit:

- `--json` mode: machine-readable summary with `{status, checks_total, checks_passed, criticals, warnings, latency_ms, rows_returned}`
- `--verbose` mode: per-check pass/fail listing
- Default human output: one line per CRITICAL/WARNING; final summary line.

Exit codes:

- 0 — all CRITICAL checks pass (WARNINGs allowed)
- 1 — at least one CRITICAL failure
- 2 — HTTP error (non-200, timeout, connection refused)
- 3 — JSON parse error
- 4 — argparse / config error

## Test plan

Unit tests in `tests/test_check_live_candidates_contract.py`:

1. **Golden path**: validator handed a fully-conformant payload (envelope + 1 candidate_review row + 1 watch row + 1 blocked + 1 data_insufficient) → exits 0.
2. **#229 regression — rich-dict counter_flags**: payload with rows whose `counter_flags = [{"flag": "dead_project", "severity": "high", "detail": "Zero commits"}]` → exits 0 (must accept rich shape).
3. **Banned-language CRITICAL — top-level**: payload with `risk_reasons = ["buy now"]` → exits 1, error message cites the offending field.
4. **Banned-language CRITICAL — nested in counter_flags.detail** (per Vector-B N2): payload with `counter_flags = [{"flag": "x", "detail": "this is a moon shot, ape in now"}]` → exits 1.
5. **Banned-language CRITICAL — uniform regardless of verdict** (per Vector-B I4): banned token inside a `data_insufficient` row → exits 1 (not WARNING).
6. **Missing meta flag CRITICAL**: payload with `meta.read_only = False` → exits 1.
7. **`not_trade_advice = False` has no escape hatch** (per Vector-B I1): exits 1 with explicit "no governance path" error message.
8. **Unknown verdict CRITICAL**: payload with `verdict = "candidate"` (the old #228 pre-fold label) → exits 1.
9. **Unknown entry_quality CRITICAL**: payload with `entry_quality = "high_conviction"` → exits 1.
10. **candidate_review invariant CRITICAL — actionable=0**: → exits 1.
11. **candidate_review + actionable=None CRITICAL** (per Vector-A N1): → exits 1.
12. **Envelope drift — missing rows**: top-level missing `rows` → exits 1.
13. **Envelope unknown top-level key WARNING** (per Vector-A I2): top-level has `{meta, rows, warnings}` → exits 0 with stderr WARNING citing `warnings` key.
14. **counter_flags item-type CRITICAL**: payload with `counter_flags = [None]` → exits 1.
15. **Schema drift warning — data_insufficient with no matching risk_reason**: → exit 0 but stderr shows WARNING.
16. **Empty rows envelope** (per Vector-A N1): `rows = []` with valid meta → exits 0.
17. **`rows_returned` mismatch CRITICAL** (per Vector-A N1): `meta.rows_returned = 3` but `len(rows) = 2` → exits 1.
18. **Malformed `generated_at` CRITICAL** (per Vector-A N1): `generated_at = "tomorrow"` → exits 1.
19. **KOL-ranking field firewall CRITICAL** (per Vector-B C3): row contains `kol_rank: 1` → exits 1.
20. **Source-rank field firewall CRITICAL**: row contains `source_score: 0.8` → exits 1.
21. **`counter_flags[i].severity` allowlist WARNING** (per Vector-B I3): item has `severity: "extreme"` (not in {high, medium, low, info}) → exit 0 with stderr WARNING.
22. **Disclaimer alternate phrasing accepted** (per Vector-B I2): `disclaimer = "informational only — not investment advice"` → exits 0.
23. **Disclaimer too-short CRITICAL** (per Vector-B I2): `disclaimer = "ok"` → exits 1.
24. **Disclaimer with zero-width-space banned-token bypass attempt** (per Vector-B N1): NFKC normalization catches `"b​y now"` in risk_reasons → exits 1.

Test fixtures use static dicts; no HTTP / no aiohttp / no real cockpit DB. The validator function takes a parsed-payload dict so the HTTP layer can be mocked or skipped entirely in unit tests.

The HTTP fetch path is a thin wrapper around `urllib.request` (stdlib) so the validator has zero new dependencies — tests cover the validator function, not the HTTP wrapper.

Test fixtures use static dicts; no HTTP / no aiohttp / no real cockpit DB. The validator function takes a parsed-payload dict so the HTTP layer can be mocked or skipped entirely in unit tests.

The HTTP fetch path is a thin wrapper around `urllib.request` (stdlib) so the validator has zero new dependencies — tests cover the validator function, not the HTTP wrapper.

## What this PR does NOT do

- Does not wire the validator into CI (deferred — operator decision; could be a post-merge follow-up).
- Does not run on a schedule (deferred — could be a cron job after manual confirmation).
- Does not write to `scout.db` or any other DB.
- Does not call any external service besides the target `<base_url>`.
- Does not change `dashboard/api.py` or `dashboard/db.py` semantics.
- Does not add a `data_insufficient` schema-drift WARNING to an alert pipeline — exits with warnings to stderr only.

## Operator runbook (will ship as docstring + brief README mention in PR description)

```bash
# Local check against locally-running dashboard:
python scripts/check_live_candidates_contract.py --url http://localhost:8000

# srilu prod smoke (via SSH two-step per AGENTS.md):
ssh root@89.167.116.187 'python3 /root/gecko-alpha/scripts/check_live_candidates_contract.py --url http://localhost:8000 --json' > .ssh_out.txt 2>&1
# Then read .ssh_out.txt

# JSON output for piping into other tooling:
python scripts/check_live_candidates_contract.py --url http://localhost:8000 --json
```

**Per Vector-B N3:** there is NO `--skip-banned-language` or `--ignore-critical` flag and there will never be. If a CRITICAL fires under time pressure, the path forward is: investigate the offending field, fix the producer (db.py / enrichment writer / model), re-run. The validator's job is to keep advice-tone out of the cockpit; bypassing it defeats the firewall.

## Backlog entry

Will file `BL-NEW-LIVE-CANDIDATES-CONTRACT-SMOKE` as SHIPPED in `backlog.md` in this PR.
