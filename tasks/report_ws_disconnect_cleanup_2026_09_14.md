# WebSocket cleanup implementation evidence

Plan3e86326c and design7cc39a0a each received independent operations and logic
approvals without folds before implementation. Work is isolated to the WS worktree;
no frozen release/helper/production changes were made.

## Change

The registered /ws/live handler now sends in the database-preparation try/except's
else arm. WebSocketDisconnect reaches the existing outer handler/finally and removes
the client instead of entering an endless retry. Transient DB/payload preparation
errors retain their five-second retry, successful payload/read limits stay identical,
and cancellation/unexpected transport errors propagate through cleanup.

Only dashboard/api.py behavior and the new focused lifecycle tests change. No new
runtime flag/dependency/task, frontend/dist build, DB/config or unit modification.

## Red and green evidence

Using C:/projects/gecko-alpha/.venv/Scripts/python.exe, Python3.12,
Uvicorn0.42.0/Starlette0.52.1/websockets16.0 (matching captured production versions):

- On old code, four lifecycle tests failed and cancellation passed: disconnect,
  transient-read-then-disconnect and unexpected transport failure timed out instead
  of completing; real Uvicorn also failed the natural-shutdown10s gate.
- The initial failing-server fixture exposed a lifespan cleanup omission. Its failure
  cleanup now explicitly shuts down that owned lifespan after forced cancellation.
  Before editing application code, the isolated old-code Uvicorn falsifier was rerun:
  failed at natural shutdown as expected,1failed/3deprecation warnings in11.05s,
  without a pending-task destruction warning. Evidence is the private automation
  artifact ws-old-shutdown-falsifier.txt. Failure cleanup is not passing evidence.
- After the minimal exception-boundary fix, all5 lifecycle tests passed in5.63s.
  The loopback integration keeps the real five-second cadence and no Uvicorn graceful
  timeout; it holds an active WebSocket while requesting shutdown, then proves serve
  completed naturally, force_exit stayed false, and ASGI tasks/client set drained.
  No production socket/DB or OS signal was used.
- Focused regression command:

```text
python -m pytest tests/test_dashboard_websocket_lifecycle.py tests/test_dashboard_api.py tests/test_dashboard_cold_start_race.py -q --tb=short
```

51passed in12.48s. Three deprecation warnings originate in the installed Uvicorn
websockets legacy adapter; no leaked-task or forced-graceful-timeout warning on
passing tests. Black targetpy312 check and git diff --check passed.

Tests exercise the actual registered endpoint, captured client set, read limits,
exact successful payload, transient DB failure, cancellation and unexpected send
error. Private test sleep substitution never replaces shared asyncio.sleep. The
natural server test uses unmodified scheduling and an owned ephemeral loopback port.

## Limits and remaining review

The fix handles send-side disconnects. It does not introduce a receiver that detects
disconnect during indefinitely failing/stuck database reads, nor bound unrelated
HTTP operations. The passing Uvicorn test does not establish systemd's stop-state
semantics; root's separate rollout guards and refreshed runtime checks remain needed.

PR583 received two independent terminal APPROVE verdicts without findings at
6a644a4561fb7dadc5954440776f2a53d5f87aee: operations reviewer ran51 tests and
verified lifecycle/natural shutdown; root's independent structural review ran49
tests and checked exception boundaries, payload/read limits, cancellation/drain
and failure cleanup. These reviews cover the four recorded vectors in
.reviewers/583.toml; the follow-up commit changes review/status metadata only.

Exact final-head CI remains required. Root owns
merge/base integration and later combined release; this task performs no deployment.
A revert would restore the known disconnect leak and must be recorded accordingly.
