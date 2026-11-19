**New primitives introduced:** NONE

# Product-readiness findings design — 2026-09-13

## Hermes-first analysis
| Domain | Hermes skill found? | Decision |
|---|---|---|
| Runtime evidence collection | none identified; hub catalog unavailable in text view | Reuse existing Gecko analyzer and GET endpoints |
| Read-only product audit | none identified in inspected directory | Repository findings document; no new runtime component |

Checks: https://hermes-agent.nousresearch.com/docs/skills/ and
https://github.com/0xNyk/awesome-hermes-agent (2026-09-13).
Awesome ecosystem verdict: general tools do not replace Gecko's cohort semantics;
reuse its analyzer without adding dependencies. Catalog search was not exhaustive.

## Evidence layout
1. State checked: baseline SHA, service state, exact UTC observations, tracked versus
   untracked priority sources. Do not claim clean production from matching SHA.
2. Drift: shipped DASH-01; partial DASH-08 and DASH-11; closed historical pool probe.
3. Current usefulness: Inbox considered vs returned, Focus source corpus; registry
   live join distinct from stale maturity. No causal inference about why paper is empty.
4. DASH-11: reuse scripts/suppression_cost_rollup.py analyze() with read-only SQLite
   connection, 7d sampling denominator and explicit 30d maturity lookback. Filter
   dispatcher suppression, pick earliest emission per token, then count r7d labels.
   Record defaults n>=10, minimum fraction 0.5 and rows/day 1 as existing script
   diagnostics, not proof of representative or tradable returns. Fraction >1 must
   be investigated, not labeled healthy by implication. Raw broad ledger counts
   remain separate. Do not publish a dollar opportunity claim or source ranking.
5. Next product slice: select using observed health, not historical date gates.
   An API/UI build remains future work and follows its own plan/design reviews.
   DASH-08 zero current paper rows reduces observed immediate benefit; does not close
   the source gap or forbid additive tests/implementation.

## Report contract
Every runtime statement is snapshot-scoped. Evidence queries are read-only GETs
or SQLite mode=ro. No config/DB mutations, vendor calls, alerts or deploy.
Distinguish fetched source SHA from running-service code proof. API results show
served behavior; a matching checkout does not prove every imported module's SHA.
Document upstream attribution/price provenance/retention uncertainties as next
verification gates rather than claiming n alone establishes production usefulness.

## Validation
Check source references, diff whitespace and Markdown-only scope. Two parallel
reviewers review plan, design, and final PR. No feature test suite is needed for
an evidence-only Markdown change; do not claim feature validation or deployment.
