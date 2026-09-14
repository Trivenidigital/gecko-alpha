# Receipt inventory review and verification — PR589

Base: 70462c169aef647c1e680ab44635b63aa4c3cd9a. Final substantive candidate: 90dd5f7d890f8ba3b7599bcde7eea972a51bfce1.

Claude safe-mode session 68e48766-6882-42fb-9393-642924930735 drafted the plan, original/replacement design and findings. All four responses returned is_error=false. Codex integrated narrower scope and reviewer folds; raw responses remain in the canonical automation directory.

| Gate | Evidence/logic reviewer | Ops/silent-failure/concurrency reviewer | Disposition |
|---|---|---|---|
| Plan 8836b14a | receipt_evidence approved | receipt_ops approved | Tail membership and cleanup details deferred to design |
| Design 668918fe | Changes required | Changes required | Duplicate-key ambiguity, 200/201 cap contradiction, observed-span wording, detached child and exception cleanup gaps |
| Replacement design d8625be4 | Approved | Approved | Single process group, reducer spawns nothing, fixed redaction/errors; non-production cleanup test required before collection |
| PR db8f1dbd | Approved with minor wording suggestion | Approved | Scoped D2 absence claim in table |
| Fold f5ae4340 | Approved | Approved | One-line narrowing verified |
| Layout 90dd5f7d | Approved | Approved | Unchanged Hermes-first section moved near primitives marker |

Verification: source references for receipt writers, JSON logging, label/prune/cache behavior inspected; git diff --check passed. Local WSL and Docker probes establish only an unavailable validation route during the attempts, not a daemon-health diagnosis. Initial production HEAD/service preflight is explicitly dated and does not verify source hashes or receipt behavior.

No reducer implementation, synthetic tests or runtime journal/metadata inventory was performed. No application tests were needed for prose. CI and final-head status are reported in GitHub and the automation result. No deployment; rollback is a docs revert. Reviewer/checklist metadata follows this substantive candidate without altering the design or findings.

Next gate: a healthy non-production Linux runner, then implement and test the approved reducer, prove process-group cleanup, and only then perform the bounded inventory. No forward capture, ranking or warning removal follows from these findings.
