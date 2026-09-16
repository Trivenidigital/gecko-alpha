# Plan: receipt inventory Linux cleanup validation

**New primitives introduced:** synthetic test harness only; no production collector.

## Hermes-first analysis
| Domain | Hermes skill found? | Decision |
|---|---|---|
| Linux process cleanup testing | Hub fetched 2026-09-16 at https://hermes-agent.nousresearch.com/docs/skills; catalog stayed loading, no applicable skill verified | Reuse GNU timeout, Python standard library and existing Ubuntu PR CI |
| Receipt inventory | Existing approved Gecko design in PR589 | Test its exact wrapper; no new collection mechanism |

awesome-hermes-agent checked at https://github.com/0xNyk/awesome-hermes-agent on 2026-09-16. Verdict: general orchestration listings do not replace testing this exact shell process group; no exhaustive absence claim.

## Selection and evidence
Base b5daecfc includes merged PR589. DASH-08/11 visibility already shipped; receipt evidence is a residual trust prerequisite. Existing source contains no receipt-wrapper cleanup test. Local Docker engine pipe is missing and WSL cannot execute python3. Existing `.github/workflows/test.yml` runs Ubuntu/Python on PRs, so local repair is not an operator dependency.

Identity-only production preflight 2026-09-16T00:23:29Z: 77751890c9f1f51ed348c365d4e7a5985ea2827d; pipeline/dashboard active. No receipts, hashes, flags, DB or journal evidence collected. Runtime conclusions remain UNKNOWN.

## Steps
- [x] Fresh-master drift, backlog/lessons, runtime identity and Hermes checks.
- [x] Two parallel plan reviews and folds.
- [x] Separate design, two parallel design reviews and folds.
- [x] Add Linux synthetic cleanup tests and an explicit short CI job.
- [ ] Open PR, two parallel PR reviews, fold findings and record clearances.
- [ ] Verify focused Linux CI; FAILED: producer and descendant survivors detected. Draft PR590 must not merge; full CI remains required.

## Acceptance and boundaries
Exercise exact `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c` wrapper against silent sleep, ignored TERM, pipe-holding descendant and stderr flood. Each case must prove fixture startup, timeout exit, wall-clock bound, and no surviving group members before fallback cleanup. A negative control must prove the detector catches a deliberately surviving child and safely removes it. Windows skips are not Linux proof.

No SSH, production data, network calls, secrets, reducer, policy or deployment changes in this increment. Passing validates only cleanup; parser tests and fresh bounded inventory remain separate gates. Rollback: revert test/CI/docs changes. No application behavior changes.
