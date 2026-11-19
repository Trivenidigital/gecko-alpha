**New primitives introduced:** NONE

# Autonomous product-readiness findings plan — 2026-09-13

## Hermes-first analysis
| Domain | Hermes skill found? | Decision |
|---|---|---|
| Repo/runtime drift audit | none found in inspected catalog; Skills Hub rendered loading state | Reuse repository code and read-only runtime queries; no custom product code in this slice |
| Dashboard trust/suppression semantics | none found in inspected ecosystem directory | Reuse Gecko primitives; Hermes remains scheduler and memory layer |

Checked https://hermes-agent.nousresearch.com/docs/skills/ and
https://github.com/0xNyk/awesome-hermes-agent on 2026-09-13. The hub's client-side
catalog did not load in the text view; this is not an exhaustive negative search.
Awesome ecosystem verdict: general orchestration/dashboard tools do not establish
Gecko-specific cohort truth; no new dependency is warranted for this findings PR.

## Goal and scope
Produce a findings PR that advances the cockpit/trust children by replacing stale
backlog assumptions with source and current runtime evidence. This is an audit
slice, not delivery of DASH-08 or DASH-11. Preserve all runtime behavior.

- [x] Refresh origin/master; read local guidance, lessons, todo, snapshot, reconciliation.
- [x] Find authoritative Fable tracker in main checkout (untracked; absent master/prod).
- [x] Parallel source drift: DASH-01 shipped; DASH-08/DASH-11 real residual gaps.
- [x] Two parallel plan reviews approved; fold exact timestamps, considered/returned counts, and separate sampling/maturity windows into design.
- [x] Design evidence structure; two parallel design reviews approved; folds applied.
- [x] Verify runtime: candidate corpus and live join; dispatcher-only suppression health/maturity.
- [x] Write findings with reproducible read-only methods, timestamps, source lines,
      limitations, and concrete next gate; add a short todo entry.
- [x] Documentation checks and existing analyzer tests passed; opened PR #570 with both labels.
- [x] Two parallel PR reviews approved; checklist/citation polish folded. Final-head confirmation and CI are recorded in the session report; no merge without full gates.

## Acceptance
Separate confirmed source gaps from runtime usefulness. No whole-token suspension
inference from one source, no enabled=1 inference of dispatch eligibility, and no
profitability claim from raw ledger-row counts. The output must name a next useful
product slice without calling a temporary empty paper corpus a permanent blocker.
No production changes, paid calls, signal flips, ranking, or source pruning.
