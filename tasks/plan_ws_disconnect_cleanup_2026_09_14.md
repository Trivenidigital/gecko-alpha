**New primitives introduced:** NONE. Correct the existing WebSocket handler's
exception boundary and test its lifecycle; reuse FastAPI/Starlette/Uvicorn.

# Dashboard WebSocket disconnect cleanup plan

## Hermes-first analysis

Drift first: current origin/master d7a0e267 has only one /ws/live route, in
dashboard/api.py:1966-2000. Its send_text at1992 remains inside the broad
Exception handler at1993, before the outer WebSocketDisconnect handler at1997.
The client set is local to create_app and removed only in the finally clause.
No existing WebSocket lifecycle tests or shutdown hook were found in tests/;
backlog/todo searches did not identify a completed matching fix. This is an
existing handler defect, not a missing broadcasting or monitoring primitive.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Existing FastAPI WebSocket disconnect lifecycle | none applicable found in accessible directory; Skills Hub catalog loading | Repair current exception boundary using existing framework behavior |
| Async regression / bounded shutdown verification | no repository-specific replacement identified | Existing pytest/asyncio and installed ASGI server; no new framework |

Checked [Hermes Skills Hub](https://hermes-agent.nousresearch.com/docs/skills/)
and [awesome-hermes-agent](https://github.com/0xNyk/awesome-hermes-agent) on
2026-09-14. The Hub returned a loading shell; the accessible ecosystem directory
lists general capabilities. Verdict: no new primitive or dependency is justified;
this is a local lifecycle correction. This is not an exhaustive negative catalog claim.

## Scope, source and review boundary

PLAN ONLY in C:/projects/gecko-alpha-ws-cleanup-20260914, branch
docs/ws-disconnect-cleanup-plan-20260914, created from freshly fetched origin/master
d7a0e2672c8d67ba33dc47cfd1a818c2b2bad5b3. Only this plan and tasks/todo.md change.
Frozen selective-release candidate07afc0ad and its worktree/helper remain untouched.
Root's autonomous bug-fix authorization applies, with Plan > two reviews > Design >
two reviews > Build > PR > two reviews/exact CI. No implementation before those
plan/design approvals. Root owns later integration and deployment decisions.

Read CLAUDE.md, relevant lessons and the actual handler/unit/dependency paths.
No repository AGENTS.md exists; supplied operator instructions apply. Follow
writing-plans/worktree/TDD guidance within the explicit review sequence.

Goal: a disconnected /ws/live connection stops polling/sending and reaches its
existing finally cleanup. Preserve successful payload keys/content, accept behavior,
five-second polling cadence and retry-on-transient-DB-error policy. Preserve task
cancellation propagation. No frontend, dist, dependencies, runtime flags, service
settings, DB/config, trading behavior, new background tasks or external messages.

## Runtime evidence and causal confidence

Root's production event at2026-09-14 02:11:23UTC logged WebSocket connection closed;
at02:11:24 Uvicorn waited for background tasks; at02:11:44 systemd's20s stop timeout
killed the old processes and left Result=timeout. Rollout never switched Git.
Baseline d2f0d61e was restored active with pipelinePID3032091 unchanged. Evidence:
automation artifact runtime-rollout-failure-state-20260914.txt. Those timestamps
describe that event only, not current production health.

The relevant code is unchanged between deployed d2 and current master. Verified
production dependency versions are Uvicorn0.42.0, Starlette0.52.1, websockets16.0.
Starlette's send converts transport OSError to WebSocketDisconnect (an Exception).
The inner catch therefore swallows it and continues the loop instead of reaching
the outer disconnect handler/finally. Independent logic reviewer reproduced two
send attempts after disconnect before external cancellation. This establishes the
handler defect. It is a high-confidence contributor to the recorded shutdown wait,
but the actual retained task stack was not captured; do not claim it was the only
possible task or that fixing it bounds unrelated HTTP/DB operations.

Uvicorn closes transports then waits for active ASGI tasks before lifespan shutdown;
the verified unit supplies no graceful-shutdown timeout. Increasing systemd timeouts
or accepting a failed-but-empty unit would address a different operational boundary
and would not fix the swallowed disconnect. Neither belongs in this code change.

Runtime assumptions for later validation: use installed versions matching the
observed stack; bind any integration test only to loopback on an ephemeral port;
all DB reads are fake or use explicitly isolated fixtures; no production DB/default
path, credentials, webhook, paid service or service restart is needed for tests.
Root must refresh deployed SHA/runtime facts before a later combined rollout.

## Proposed minimal behavior change

Narrow the retryable exception scope to database/read-payload preparation, allowing
transport disconnect from send_text to reach the existing outer termination/cleanup
path. Design may choose an equally small explicit disconnect re-raise before the
broad retry catch if that better preserves the current boundary, but must distinguish
DB failure from transport closure and demonstrate the real handler exits.

Do not add a second receiver/broadcaster task, registry API, new timeout/config flag,
custom close protocol, or broad catch around cancellation. Unexpected transport or
programming exceptions must not become an endless retry loop. Keep final client-set
discard idempotent and ensure cancellation still unwinds through finally.

Expected implementation files: dashboard/api.py, one focused
tests/test_dashboard_websocket_lifecycle.py, and plan/design/report/todo metadata.
If meaningful verification needs more than these boundaries, present the concrete
reason during design review rather than silently expanding scope. No bundle rebuild
is necessary for a Python handler change; root can carry the eventual merged API
blob into a newly reviewed selective release alongside the existing viewers.

## Tests and acceptance after approvals

- Exercise the actual registered /ws/live endpoint from create_app, not a duplicate
  implementation or text-pattern test. A fake WebSocket accepts then raises the real
  WebSocketDisconnect from send_text. Assert exactly one send attempt, bounded normal
  task completion without forced cancellation, and removal from its real captured
  client set. The old handler must fail this test; avoid relying solely on elapsed
  timing by using explicit events/counters.
- Cancel a live handler while waiting/polling and assert cancellation propagates,
  the client set empties and no owned task remains. Test cleanup after a transport
  error without turning arbitrary errors into permanent DB retries.
- Make a DB read fail once and then succeed, with no production DB calls. Assert
  the handler remains live, resumes one expected update payload and preserves the
  five-second cadence argument. Then disconnect and verify cleanup. Test fixtures
  must not globally replace asyncio.sleep in a way that masks server scheduling.
- Verify representative successful payload keys/values remain identical. Tests
  should fail if the disconnect termination is removed or the DB retry is broken;
  do not merely restate the new exception syntax.
- If feasible with installed dependencies, run one bounded loopback Uvicorn check
  using the actual app and fake DB readers: establish an active WebSocket, request
  server shutdown, and prove serve/connection tasks drain without forced cancellation
  or systemd/SIGKILL. Keep the normal five-second cadence; allow a small explicit
  test deadline (for example10s, with separate bounded failure cleanup). It must
  fail on the old handler and must not pass because the test/server forcibly cancels
  the stuck handler. Design will specify exact ownership/teardown and portability.
  If unavailable, report that limit and retain direct-handler behavioral proof;
  do not claim full production graceful shutdown from a mocked send alone.

## Work sequence and verification

- [ ] Root obtains two independent plan approvals (handler/lifecycle correctness
  and shutdown/runtime boundaries), folds feedback and records exact SHA.
- [ ] Write design specifying the narrow exception placement, actual route fixture,
  cleanup/cancellation assertions and feasible bounded ASGI shutdown test. Obtain
  two design approvals before implementation.
- [ ] Write the failing disconnect regression first; record old-code falsifier.
  Implement only the approved handler correction and run the focused lifecycle
  tests. Commit the meaningful verified code/test slice.
- [ ] Run relevant existing dashboard API/cold-start tests and applicable contract
  checks, then exact final-head CI. Record actual commands/results and scope limits
  in a report. No production smoke/restart in this worktree task.
- [ ] Open the PR with codex/codex-automation labels where available; two independent
  PR reviewers and required findings/falsifiers must reach terminal approval before
  authorized merge. Root handles refreshed-base integration and later release.

Proposed focused command after implementation: python -m pytest
tests/test_dashboard_websocket_lifecycle.py tests/test_dashboard_api.py
tests/test_dashboard_cold_start_race.py -q --tb=short, plus any specifically affected
contract tests. Check formatting and git diff --check. Do not repeat broad tests
unless changes or unresolved failures justify them. No new check is claimed passing.

Rollback after a future merge is a normal code revert, but it restores the known
disconnect defect; record that consequence. This plan neither authorizes production
actions nor weakens the rollout's physical-quiescence/policy/schema invariants.
