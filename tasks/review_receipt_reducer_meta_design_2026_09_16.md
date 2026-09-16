# Receipt reducer/META design review — 2026-09-16

## Plan gate completed

PR [#591](https://github.com/Trivenidigital/gecko-alpha/pull/591) merged at
2026-09-16T18:37:27Z as `363b9614bdf946cef5b9612961ee73b3c69f9839`.
Reviewed head `dd3251da1bee937a8e0bf17313c8b47b3b16eb30` retained both terminal
independent approvals. CI run 35124293471 attempt 2 passed all four checks.
The earlier full-suite timeout is superseded by this successful retry; no
workflow timeout or executable source was changed. No deployment was needed.

## Design draft and independent review

Configured safe-mode Claude session `5e207c93-18ab-4dd7-8a41-56497f382438`
returned a read-only design draft with `is_error=false`; the coordinator saved
it at `3284233791ca3f68646a23cdc25acea226bea481`. No implementation was produced.

Two parallel reviewers independently returned **REQUEST CHANGES**:

| Reviewer/vector | Required fold |
|---|---|
| design_logic: parsing/consumer correctness | Exclude structural `event` from unknown-key counts; enforce reducer conservation, domain and span invariants; enforce META success consistency and slot rules; reject non-finite elapsed values and non-integer return codes; classify partial tails before parsing |
| design_ops: filesystem/operational safety | Make guard falsifiers reach the actual open; isolate blocking mutations in bounded subprocesses with cleanup; narrow change-detection claims; enforce a cumulative cap-plus-one read budget |
| Both | Earlier realpath/lstat rejection masks proposed O_NOFOLLOW/O_NONBLOCK mutations; tests must discriminate each guard |
| design_logic: scope | Explicitly review the bounded addition of fixed presence/unknown counters to the approved schema; pin the output-bound constant |

The configured author was asked to fold these findings in the design file only.
**Design approval remains pending. No build or production collection is authorized
by this review record.** Separate implementation and PR review gates remain.

## Runtime and existing artifacts

Read-only observations at approximately 18:38 UTC: production remains selective
`77751890c9f1f51ed348c365d4e7a5985ea2827d`, tracked clean. Pipeline and dashboard
were active. Correct unit inventory showed `hermes-gateway.service` active;
the initially queried `hermes` name was incorrect and is excluded from health
conclusions. Registry endpoint returned HTTP 200. These are process/HTTP facts,
not evidence of data freshness or worker completion.

`codex-autonomous-dev-srilu.service` remains failed at its pre-start auth guard:
13:25:33 UTC, exit 21. The timer is active. Main-process exit zero is irrelevant
when the pre-start command failed. Restoring the intended worker OAuth login is
an operator account action; this design work does not require that change.

The local autonomous status reporter exited zero. Requested templates, role map
and bounded cockpit/trust surfaces already exist; no parent rebuild is scoped.
Historical first-run evidence is retained in the earlier reviewed report; this
run does not reinterpret the current auth failure as never having run.

## Boundaries

No deployment, receipt collection, DB/config/account/vendor/trading changes or
message sends. Paid vendors, activation, execution, sizing, pruning and destructive
changes retain operator gates. Broader backlog children are not claimed exhausted.
Permanent prompt adjustment remains recommended: consult both automation memories,
verify current ownership, retire superseded CI blockers and distinguish plan,
design, implementation, collection and deployment gates. Configuration unchanged.

## Design gate completed

Configured author revision2 folded the findings at5284e6ee. Both reviewers accepted the bounded fixed-schema amendment, then requested three precise test-spec corrections. Coordinator corrected parser call count, staged-growth read reachability, ninth-slot rejection reason and narrowed the retained growth claim atfa032eea374b3c33ebf554b5288c9ded87895ef4.

Both design_logic and design_ops returned terminal APPROVE on fa032eea, explicitly accepting Fold7. No design findings remain. This supersedes the pending design status above only; implementation tests and independent PR reviews remain pending. No production collection is authorized.


## Implementation candidate and first PR reviews

Configured author completed five files at406ed0109b840f1fabcb12ba1acaae0ad2e4eba5. Coordinator independently reproduced153 passed/10 skipped/93 subtests (Python3.14); author recorded126 unittest cases/39 Linux/platform skips on3.12. Author mutation report:33 killed,4 Linux-only skipped, no claimed Linux proof. Source restored after each local mutation. An initial uv probe attempted dependency sync and failed TLS; no installation success or production change is claimed.

Both independent PR reviewers returned REQUEST CHANGES. design_logic reproduced math.isfinite on a400-digit JSON elapsed integer raising OverflowError in the consumer oracle instead of fixed rejection. design_ops found no META source defect but requires four actual Linux mutant kills (realpath, O_NOFOLLOW, O_NONBLOCK, post-open regular-file check). Both explicitly accepted the portable errno import amendment.

Configured author owns only the two affected test modules for the fold. Ops explicitly accepts a Linux-only temporary-copy mutation proof in the existing META module, with bounded subprocesses and expected assertion failures rather than skips/import errors/timeouts. No workflow or production changes. Final PR approvals and exact-head CI remain pending.

