# Plan: BL-NEW-LIVE-DECISION-COCKPIT (V1) — `/api/live_candidates` (read-only)

Date: 2026-05-23

## Goal

Advance `BL-NEW-LIVE-DECISION-COCKPIT` by shipping a **single read-only endpoint** that answers:

> “What 3–5 tokens look plausibly tradable *now* for a tiny experiment, and why?”

V1 is **visibility-only**: no live execution, no sizing, no suppression, no pruning.

## Drift-check (in-tree)

- Grep for an existing endpoint/panel: `live_candidates` / “Now Tradable”.
- Result: no implementation found; only backlog references in `backlog.md` and a mention in `tasks/todo.md`.
- Fold: no exact match exists, but the paper-trading substrate is already present (read-only joins of `paper_trades` ↔ `price_cache`). Build `/api/live_candidates` as a thin grouping + labeling layer on top, not a parallel query stack.

## **New primitives introduced**

- `dashboard/db.py`: `get_live_candidates(...)` (read-only query + deterministic labeling)
- `dashboard/models.py`: `LiveCandidateResponse`
- `dashboard/api.py`: `GET /api/live_candidates`
- `tests/`: new endpoint test module seeded via `scout.db.Database.initialize()`

## Hermes-first analysis

Drift-check above is negative, so proceed to Hermes ecosystem check.

Domains checked against the public Skills Hub on 2026-05-23 (each named as a
generic capability, not a project-specific contract — see global CLAUDE.md
§7b):

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trader candidate explanation over structured DB rows | none found | Build from scratch; optional Hermes enrichment later |
| Counter-risk interpretation over structured fields | none found | Use as enrichment only (future); V1 does not hard-gate on it |
| Social/KOL context normalization into a trader cockpit | none found | Keep context-only; do not rank/boost candidates in V1 |
| Price truth / PnL / identity / execution | N/A (must never be Hermes-load-bearing) | Keep custom (DB truth only) |

**Deployed-surface check (operator-gated, blocking before merge):** the
public hub check above is necessary but not sufficient — `~/.hermes/skills`
on the deployment host may contain an operator-installed skill that the hub
does not list. The operator MUST paste the output of the commands below into
the PR before merge; if any new skill name surfaces, this V1 design has to
be re-checked against it. The repo sandbox cannot execute these commands
itself.

```bash
ls -la ~/.hermes || true
ls -la ~/.hermes/skills || true
ls -la ~/.hermes/cron || true
test -f ~/.hermes/cron/jobs.json && jq '.jobs | keys' ~/.hermes/cron/jobs.json | head
```

Hermes docs reference points:
- Skills hub: https://hermes-agent.nousresearch.com/docs/skills/
- Skills system overview: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/

## Runtime-state verification (required before trusting scores)

This change is read-only, but its usefulness depends on current runtime tables/columns and join invariants. Before deploy/merge, verify on the target `scout.db`:

1. `paper_trades` columns exist: `token_id`, `symbol`, `name`, `chain`, `signal_type`, `signal_data`, `entry_price`, `opened_at`, `status`, `actionable`, `would_be_live`.
2. `price_cache` columns exist: `coin_id`, `current_price`, `price_change_24h`, `market_cap`, `updated_at`.
3. Optional enrichments present (endpoint should degrade gracefully when absent):
   - `predictions` with `coin_id`, `narrative_fit_score`, `counter_risk_score`, `counter_flags`, `predicted_at`
   - `chain_matches` with `token_id`, `pipeline`, `pattern_name`, `completed_at`

Suggested operator commands (read-only) — **proof queries, not just schema**:

```bash
sqlite3 scout.db ".schema paper_trades"
sqlite3 scout.db ".schema price_cache"
sqlite3 scout.db ".schema predictions"
sqlite3 scout.db ".schema chain_matches"
sqlite3 scout.db "SELECT COUNT(*) FROM paper_trades;"
sqlite3 scout.db "SELECT COUNT(*) FROM paper_trades WHERE opened_at >= datetime('now','-36 hours');"
sqlite3 scout.db "SELECT COUNT(*) FROM price_cache;"
sqlite3 scout.db "SELECT typeof(opened_at), COUNT(*) FROM paper_trades GROUP BY 1;"
sqlite3 scout.db "SELECT COUNT(*) FROM paper_trades WHERE entry_price IS NULL OR entry_price <= 0;"
sqlite3 scout.db "SELECT COUNT(*) FROM paper_trades pt JOIN price_cache pc ON pc.coin_id = pt.token_id WHERE pt.opened_at >= datetime('now','-36 hours');"
sqlite3 scout.db "SELECT MIN(updated_at), MAX(updated_at) FROM price_cache;"
```

## API contract (V1)

`GET /api/live_candidates?limit=20&window_hours=36`

Returns a list of per-token rows:
- Token identity: `token_id`, `symbol`, `name`, `chain`
- Price snapshot: `current_price`, `market_cap`, `price_change_24h`, `price_updated_at`, `price_is_stale`
- Paper-trade evidence: `open_trade_ids`, `recent_trade_ids`, `surfaces` (distinct `signal_type`), `actionable`, `would_be_live`
- Prediction/counter-risk (optional): `narrative_fit_score`, `counter_risk_score`, `counter_flags`
- Verdict fields:
  - `verdict`: `candidate` | `watch` | `blocked` | `data_insufficient`
  - `entry_quality`: `fresh_entry` | `acceptable_pullback` | `already_faded` | `already_ran` | `too_stale` | `data_insufficient`
  - `inclusion_reasons`: list[str]
  - `risk_reasons`: list[str]

## Deterministic V1 verdict rules (pre-registered)

Inputs are limited to structured DB fields (no LLM calls). V1 is explicitly **non-execution** and should ship with an operator-visible disclaimer: “read-only labels; not trading advice; triggers no actions”.

### Candidate set (pre-registered)

- Primary cohort is **open paper trades only**: `paper_trades.status='open'`.
- Closed/history rows are optional context only (future follow-up).

- Hard gates → `data_insufficient`:
  - missing `price_cache` row OR missing `entry_price` OR `opened_at` unparsable
  - extreme stale price (`now - price_cache.updated_at` > 2h). Fold: also expose `price_is_stale` when age > 1h as a warning label (align with paper-trading engine freshness).
- Entry-quality:
  - compute `pct_from_entry = (current_price - entry_price)/entry_price * 100`
  - `fresh_entry`: -2% .. +8%
  - `acceptable_pullback`: -6% .. +15%
  - `already_ran`: > +25%
  - `already_faded`: < -10%
- Verdict:
  - `candidate`: actionable==1 AND would_be_live==1 AND entry_quality in {fresh_entry, acceptable_pullback}
  - `watch`: actionable==1 AND would_be_live!=1 (0/NULL), OR entry_quality borderline
  - `blocked`: actionable==0

Fold (V1 safety): `predictions.counter_risk_score` must be enrichment-only until coverage/range are verified on the target DB. It may add `risk_reasons` and downgrade `candidate` → `watch`, but must not be the sole reason for `blocked`.

These thresholds are intentionally conservative and are **UI labels**, not trading rules.

## Implementation steps

1. Add DB query + aggregation in `dashboard/db.py` (read-only).
2. Add Pydantic model in `dashboard/models.py`.
3. Add `GET /api/live_candidates` in `dashboard/api.py` with parameter caps (`limit<=50`, `6<=window_hours<=72`).
4. Add tests seeding SQLite via `scout.db.Database.initialize()` (avoid hand-rolled fake schema drift).
5. (Optional follow-up, separate): frontend “Now Tradable” panel once Node deps/build are available in the environment.

## Acceptance (V1)

- One curl call returns 3–5 plausible experimental candidates in <2s on a typical `scout.db`.
- Every candidate row has an explicit `verdict`, `entry_quality`, and reasons.
- Endpoint is read-only (no DB writes) and degrades gracefully when optional tables are missing.
