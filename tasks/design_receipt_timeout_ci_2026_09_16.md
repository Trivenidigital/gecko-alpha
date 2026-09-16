# Design: synthetic receipt timeout CI proof

**New primitives introduced:** test-only worker and unittest cases, no collector.

## Hermes-first analysis
| Domain | Hermes skill found? | Decision |
|---|---|---|
| Process lifecycle testing | Hub checked 2026-09-16; catalog loading, no skill verified: https://hermes-agent.nousresearch.com/docs/skills | Standard-library tests of existing GNU wrapper |

awesome-hermes-agent checked https://github.com/0xNyk/awesome-hermes-agent on 2026-09-16; no listed orchestration capability replaces empirical wrapper cleanup testing. Reuse existing CI, no dependency install.

## Harness
`tests/test_receipt_inventory_timeout.py` runs under unittest or pytest. Linux-only cases launch that same file as an isolated worker with a 25-second parent deadline. Windows skips explicitly; missing Linux prerequisites fail. Each worker enables Linux child-subreaper via prctl, so killed grandchildren can be reaped without relying on runner PID1. This state is confined to the worker, not the pytest process.

The worker starts the exact argv `timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c <pipeline>` with `start_new_session=True`, stderr DEVNULL, stdout DEVNULL. Timeout's PID is the known group ID and must differ from worker's group. Pipeline uses synthetic Python producer, `head -c 2097153`, and Python stdin reader. Reader spawns nothing. Readiness files in TemporaryDirectory record producer/descendant PID and PGID after setup; no external data is read.

Cases: silent producer; producer ignoring TERM; producer with pipe-holding child ignoring TERM (producer also ignores TERM to keep timeout supervising through KILL); producer flooding stderr with fixed chunks. Readiness deadline 4s. For descendant case require both readiness records and their PGIDs equal wrapper PID. Timeout completion deadline 16s from launch; assert return 124 or -9/137, 9 <= elapsed <= 16. This tolerates runner scheduling while preserving a failure ceiling. Python represents direct SIGKILL as -9, equivalent to shell 137.

After wrapper exits, reap only owned adopted children (`waitpid(-pgid, WNOHANG)`), polling at most 2s. Check `pgrep -g PGID`; exit 1 is empty, exit 0 means survivors and fails, anything else is a harness error. Inspect before fallback cleanup; fallback cannot turn failure into success. Zombies may be reaped but no live process is killed before the assertion. A finally block kills only this known isolated process group if still present, waits/reaps for a bounded interval and removes temporary fixtures.

Negative control: launch a deliberate long-lived synthetic producer in its own group; await readiness, assert the same empty-group check rejects it, then finally kill/reap and assert empty. This demonstrates detector sensitivity without altering production wrapper. The harness never uses SSH or reads runtime config.

## CI and failure limits
Add a dedicated job in existing test.yml: ubuntu-latest, contents:read, checkout persist-credentials:false, Python3.12, timeout-minutes:5, run `python -m unittest discover -s tests -p test_receipt_inventory_timeout.py -v`. No dependency or secret injection. Normal full pytest also discovers these tests. Worker outer deadline failure cleanup uses a recorded wrapper PGID file so parent can kill the known group, never its own. Test runner and CI timeouts are backstops, not passing evidence.

## Evidence and follow-on gate
Log unittest case results and synthetic elapsed/exit/group-empty receipts. Passing proves only the exact timeout wrapper with these synthetic fixtures. It does not prove reducer parsing, source identity, receipt availability or production timeout behavior. Pure reducer/META tests and fresh production preflight remain required before the approved bounded read. Revert tests/job/docs to roll back; no deployment needed.

## Plan reviews
4736f19b approved independently by blocker_review (logic/test validity) and timeout_ops (ops safety). Design folds: readiness and PGID guards, bounded reaping, per-worker subreaper, before-fallback survivor assertions, least-privilege CI.
