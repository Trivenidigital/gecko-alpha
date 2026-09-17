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

## Dependency completed

PR592 merged2026-09-16T19:45:45Z as5281f047346a951acb05926a27fe0489bc3cafd4. Exacthead65d93065 CI35140345937 passed8302tests/14skips, allfourjobsSUCCESS including Linux mutation self-checks. No deployment or collection.

## Design review findings — no build

Two independent DESIGN reviews rejected0821bad3 (checklist missing actual rc/immutablestate/enforceablelimits/sourcepins and safequoting). Configured author revision15216a1f selects the plan-permitted fixedsequence driver with output-to-file then evaluate. Both reviewers REQUEST CHANGES again: correct nested shell quoting; distinguish parser acceptance limit from physical storage bound; prove local transport descendant cleanup; persist in-flight state for everyoperation and exclusive run ownership; bind stagedscripts to approved revision; use test-owned process identities; deterministic publicationfailure; saturation means completenessunknown. Revision3 requested from configuredauthor. DESIGN REMAINS UNAPPROVED; no source/test implementation, staging or receiptcollection.

Fresh read-only runtime19:50:31Z remains selective77751890 trackedclean; pipeline/dashboard/Hermesactive, registryHTTP200. Newworkerprestartattempt19:22:35Z authguardexit21, timeractive. Operator restore intendedVPSOAuth; this is separate from integrationengineering. No account/service changes.


## 2026-09-16 23:50 UTC — Amendment review, not design approval

Configured author ee1f8219-ef8c-4049-8de5-1189922eae4c recovered after availability probe succeeded. Amendment candidate a578266220d9ae40bf2af6dbc16d61c9a045cebc was reviewed by two independent parallel agents. Both REQUEST CHANGES.

| Reviewer/vector | Required folds |
|---|---|
| plan_structure / structural | OpenSSH security-key helper invalidates exhaustive childlessness claim; remote head does not bound login/interpreter diagnostics; buffered truncation may exit0 so reject sentinel regardless of rc; clear IN_FLIGHT atomically with next/verdict/permanent attempt ledger and durable state |
| plan_operations / operational | Binary-safe Windows git object materialization; disclose whole-capture bound unproved; manual busy removal only after verified owner and transport termination; qualify build permission with separate design reviews |

Configured author received all folds; no plan/design/build approval is inferred. Physical whole-capture bounds and local descendant containment must remain prerequisites unless proved by a separately reviewed design. Fresh source of SSH helper finding: https://github.com/openssh/openssh-portable/blob/master/sshconnect2.c and https://github.com/openssh/openssh-portable/blob/master/ssh-sk-client.c.

Independent ci_diagnostic confirmed no ready non-production local Linux runtime: Docker engine unavailable, only internal docker-desktop WSL without Python/uv. Existing CI has no manual subset trigger. Actual failed CI merge SHA94b87a4ebe24dc6da1c3f2e86366224e0eb0d32b; firewall118tests took57.07s versus16.50s in green35143596029, same Ubuntu24.04 image20260907.300.1/CPython3.12.14. Broad slowdown observed; cause unproven. A five-minute verbose firewall subset is the next diagnostic once a reviewed execution route exists; no third full-suite retry requested.

Both plan_structure and plan_operations returned terminal APPROVE PLAN on ccbbab34ba664ccbc42e40e3f5389f912b4ddfe1 after configured author folds. Approval is prerequisite-scoping only: PREREQ-1 containment, PREREQ-2 whole-capture bounds and unsupported platform durability remain unresolved. Coordinator added explicit file-flush/fsync-before-replace, directory persistence where supported, and fault-test boundaries. No remaining plan review folds. Separate design assessment assigned to the configured author; no implementation or production authorization.
