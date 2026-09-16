# Receipt inventory integration reviews — 2026-09-16

## Dependency and scope

PR592 final candidate65d93065 has two terminal independent approvals; exact-head
Linux CI is still pending at this checkpoint. Integration planning proceeds on a
separate branch. No production staging, collection or deployment has occurred.

Configured Claude session5e207c93-18ab-4dd7-8a41-56497f382438 authored the plan
and two revisions. The coordinator saved the initial draft atf5a356a7, revision2
at446d91af, revision3 at56ff278c and final cleanup fold at035cd914.

## Parallel plan reviews and folds

| Vector | Findings folded |
|---|---|
| design_logic: evidence/interpretation | Separate cleanup failures from missing retention evidence; require validated pre-window span; unusable output is observation unavailable; distinguish current checkout/process from historical producer; restore dirty-checkout stop |
| design_ops: operational safety | Independently hash all scripts before execution; license publication scratch files; bound sanitized controls; replace invented interactive protocol with standard-tool sequence; declare possible glue/test primitives; forbid signalling on guessed group identity |
| Both | Separate normal proof-backed cleanup from lost-session recovery; every attempted observation must have affirmative cleanup proof, preventing vacuous success before pgid publication |

Both reviewers returned terminal **APPROVE PLAN** on
`035cd9142098826361bbd5d7c213a11ed38fa10a`. This authorizes separate design only.

## Design stage

The configured author has been assigned only the design file. Choice of a manual
checklist versus a disposable sequence driver/test remains subject to two design
reviews, including exact file scope, bounded transport and synthetic end-to-end
validation. No implementation or runtime authorization is inferred from the plan.

D1–D4 remain UNKNOWN. Retaining one pre-window journal record does not establish
continuous retention, independent attempts, cross-store identity or price lineage.
Current process identity does not establish historical loaded source bytes.
