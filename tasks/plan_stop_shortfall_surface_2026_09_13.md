# DASH-09 historical entry-stop display plan

**New primitives introduced:** one pure display eligibility/calculation helper;
reuse history endpoint, read-only DB connection, frozen entry snapshots and table UI.

## Hermes-first analysis

Checked 2026-09-13 after source drift audit.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Historical paper-price attribution | none found in accessible catalog; hub remains client-side loading | Build repository-specific eligibility using existing stored evidence |
| Closed-trade dashboard | none found for Gecko contracts | Extend existing endpoint/table; no external runtime dependency |

Skills hub: https://hermes-agent.nousresearch.com/docs/skills/ .
Awesome ecosystem checked: https://github.com/0xNyk/awesome-hermes-agent .
Verdict: generic orchestration/surfaces do not replace this repository's frozen
entry-stop provenance contract; Hermes remains orchestrator. Catalog check is
limited, not a claim that all ecosystem skills were exhaustively searched.

## Scope and evidence

Current master dd0373ee; isolated branch feat/overnight-closeout-20260913-2215.
Source history query (dashboard/db.py:3202) omits entry-stop evidence; current
closed-trade table has no comparison column. Existing findings are documented
in tasks/findings_stop_shortfall_2026_09_13.md. Do not rebuild the cockpit parent.

Runtime 2026-09-13T22:19:37Z: mode=ro/query_only snapshot confirms required
paper and snapshot columns; cutover 2026-07-03T00:32:14.634856+00:00;
355 closed_sl rows (27 market/cg_lane/v1, 108 market/legacy/v1,
216 market/legacy/no snapshot, four stop_gap_model/cg_lane/v1).
No open paper trades. Last stop August 9. Source and current value are verified;
the existing endpoint reaches stored history independent of dispatch flags.
New event rate is zero; this historical display needs no forward soak.
Production HEAD d2f0d61e, tracked clean; actual pipeline/dashboard/Hermes units active.

## Implementation sequence

- [x] Drift, ownership and runtime preflight; capture task remains separately owned.
- [x] Two parallel plan reviews, fold findings.
- [x] Write design; two parallel design reviews, fold findings.
- [x] Tests first: strict eligibility/arithmetic, incomplete schema retains history,
  actionability/pagination unchanged, modeled and unavailable states visible.
- [x] Add per-row stop_shortfall object and nonsortable column to existing table.
- [x] Run focused dashboard tests, frontend build/contracts and visual smoke.
- [x] Commit and create PR; two parallel PR reviewers across structural and
  attribution/ops vectors; fold every finding before merge.
- [ ] Exact-head CI green; merge permitted read-only PR. Deployment only with
  verified ownership, bounded smoke and rollback notes; otherwise retain merged
  artifact and identify deployment owner explicitly.

## Contract and boundaries

UI/API explicitly label historical PAPER / EXPERIMENTAL, not for pruning,
sizing or dispatch decisions. Stored exits already include modeled paper
slippage; this display cannot attribute venue execution or fees.
Every unavailable row carries a specific exclusion reason.
Metric is recorded entry-stop shortfall in percentage points, not execution
slippage, terminal-stop overshoot or realized portfolio PnL. Formula:
max(0, -100*(exit_price/entry_price-1)-sl_pct_at_entry).
Keep original rows and PnL unchanged. Eligible only for explicit closed_sl/market,
nonempty nonlegacy price source, v1 snapshot, known cutover/close at-or-after,
finite valid prices/stop/quantities, no conviction or ladder fills, remaining
quantity equal original, zero realized partial PnL, consistent entry notional.
Missing schema/evidence produces unavailable; modeled provenance stays modeled.
No aggregate or rank in this slice: all-history aggregate remains a separate
residual and must not be represented by the paginated sample mean.
No DB migrations/writes, dispatch changes, messages, paid APIs or configuration.

## Authorization

Automation production-push request authorizes reviewed read-only dashboard/API
implementation, feature push, CI-green two-vector merge and verified deployment.
No operator-only gates are crossed. Plan/design reviews implement the explicitly
requested autonomous approval workflow.
