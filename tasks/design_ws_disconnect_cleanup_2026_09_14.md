**New primitives introduced:** NONE. Narrow an existing retry boundary; add
behavioral tests using installed FastAPI/Starlette/Uvicorn and pytest facilities.

# WebSocket disconnect cleanup design

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Existing ASGI handler disconnect cleanup | no applicable replacement in accessible directory; Hub catalog loading | Existing framework exception and finally cleanup |
| Async lifecycle / shutdown regression | no repository-specific replacement identified | Installed pytest, Uvicorn and websockets; no additional dependency |

Drift and 2026-09-14 checks are recorded in the approved plan. Sources:
[Hermes Skills Hub](https://hermes-agent.nousresearch.com/docs/skills/) and
[awesome-hermes-agent](https://github.com/0xNyk/awesome-hermes-agent).
Verdict: correct local control flow, not a new WebSocket orchestration primitive.
The Hub loading shell does not support an exhaustive negative catalog claim.

## Approval and scope

Root reports independent operations and logic plan approvals without folds on
3e86326cae1f0d409c2c42b250e5f4a06a69322f. This is DESIGN ONLY, awaiting two
design approvals before tests or implementation at the time it was written.
Both independent operations and logic design reviews subsequently APPROVED
7cc39a0acc027c8d65564516fb9d6f86ef738372 without folds; root authorized build.
Isolated worktree
C:/projects/gecko-alpha-ws-cleanup-20260914 remains based on master d7a0e267.
Frozen release07af, rollout helper, production services and other worktrees are
unchanged. Only this design and task status change now.

Implementation is confined to dashboard/api.py's registered /ws/live handler and
tests/test_dashboard_websocket_lifecycle.py, plus report/status documents. No frontend
or generated bundle, API payload schema, dependencies, DB/config, service flags,
timeout values, receiver task or broadcaster architecture change.

## Existing control flow and minimal correction

The handler accepts a WebSocket, registers it in create_app's `_ws_clients`, then
reads five dashboard query results, serializes a payload and sends it. The inner
`except Exception` currently includes `send_text`; this swallows the framework's
WebSocketDisconnect and causes another poll/send. Its outer disconnect handler and
finally discard cannot finish while that loop keeps running.

Move only the transport send outside the inner read/serialization retry scope.
Use the try/except's `else` arm for `await ws.send_text(payload)`, so a failed read
never sends an undefined/stale payload. Keep the existing outer
`except WebSocketDisconnect: pass` and `finally: _ws_clients.discard(ws)`.
Keep the five-second sleep after the read-or-send attempt in normal operation.
Update the local retry comment to describe DB/payload preparation, not transport.

Consequences: successful updates remain identical; transient DB/read serialization
errors still wait five seconds and retry; a transport disconnect reaches the outer
handler immediately and skips the next sleep/poll; cancellation remains uncaught
and unwinds finally; unexpected send/programming exceptions propagate and clean up
instead of silently retrying forever. Do not catch arbitrary RuntimeError by message
or broaden framework exception handling. Do not call close again on an already
closed socket solely to clean the registration set.

This fixes observed send-side disconnects. It does not introduce a receiver that
observes a disconnect during an indefinitely failing/stuck DB read; healthy reads
reach the next send after the existing cadence. It does not bound unrelated HTTP
requests, native SQLite calls or every possible server shutdown delay. Those limits
must be stated in the report rather than hidden behind a global timeout adjustment.

## Direct-handler test fixture

Construct the real application with an explicit tmp_path DB pathname. Locate the
registered WebSocket route by path and use its actual endpoint callable. Obtain
its existing client set through inspect.getclosurevars(endpoint).nonlocals; tests
may inspect that set without introducing any production accessor/state API.

Patch only the five invoked dashboard.db readers (get_status, get_candidates,
get_funnel, get_signal_hit_rates, get_recent_alerts) with deterministic async fakes.
No fake invokes SQLite or the cached ScoutDatabase initializer. Each fake records
its expected path/limit; return distinguishable payload values. Settings use existing
test dummy values and no production .env/path. App construction must not invoke
external actions; only the WebSocket endpoint is exercised.

For deterministic direct tests, replace the dashboard.api module's reference to
asyncio with a private proxy that preserves the real module attributes but supplies
a controlled sleep. Do not monkeypatch asyncio.sleep on the shared module object.
Controlled sleep records its argument and uses real asyncio events/sleep(0) to yield.
The actual Uvicorn integration below uses unmodified asyncio and the real cadence.

Tests:

1. A fake socket accepts and raises the real WebSocketDisconnect on its first send.
   Await endpoint completion through a shielded task with a short outer test deadline.
   Assert one accept/send/read cycle, no retry sleep, no cancellation on the passing
   path, and empty registered client set. Old code times out or records a second
   attempt; cleanup of that failing case is separate from the success assertion.
2. A read fails once, then all five readers succeed. Assert sleep argument5 after
   the failure, no stale send, an exact expected serialized update payload on the
   next iteration, then a real disconnect ends the task and empties the client set.
3. Cancel the actual task while its controlled sleep/read is blocked. Assert
   CancelledError propagates and client removal happens. No swallowed cancellation
   or lingering owned task is accepted.
4. An unexpected send RuntimeError propagates and removes the client; this prevents
   a broader transport-exception retry from restoring the leak under another name.

All fixture-owned tasks are tracked and drained in finally. Cleanup may cancel a
failing task under a separate short deadline, but such cancellation never counts
as successful lifecycle completion. Tests fail if the send is returned inside the
retry catch or the existing client discard is removed.

## Installed-dependency natural shutdown integration

Observed production versions: Uvicorn0.42.0, Starlette0.52.1 and websockets16.0;
the existing project environment matches them. Use those installed packages, with
no install or dependency changes. Record actual versions at verification time.

Use one pytest-asyncio test with a real uvicorn.Server and the actual application,
plus the same deterministic fake DB readers. Reserve an IPv4 loopback socket using
bind(('127.0.0.1',0)) and pass that owned socket to `server.serve(sockets=[sock])`.
Never select port8000 or bind0.0.0.0. Configure the existing websockets protocol,
disable access-log noise and use `log_config=None`; leave graceful-shutdown timeout
at its default None. This is a test server only, not a production config change.

Replace only this server instance's capture_signals context with nullcontext to
avoid process-global signal changes inside pytest. Drive the normal shutdown flag
`server.should_exit=True`; do not send an OS signal, call force_exit on success or
set a graceful timeout that would cancel away the bug.

Ownership and bounds:

- Start one owned serve task. Wait for server.started with a3s startup bound.
- Open the installed websockets.asyncio.client connection to the ephemeral URL
  with a2s handshake bound; wait at most2s for one real update and verify its payload.
  Retain the client connection while requesting server shutdown, reproducing a
  live viewer at stop. The first frame establishes that the ASGI handler is active.
- Set should_exit. Await the shielded serve task for at most10s. The real five-second
  cadence remains; after transport shutdown, its next send must terminate the
  handler naturally. Success requires serve task completed without cancellation,
  server.force_exit is false, server_state.tasks is empty, and route client set is
  empty. Any graceful-timeout cancellation log also invalidates success.
- In finally, close the client with a1s bound and close the owned socket. Only if
  natural completion failed, set force_exit and cancel the explicitly tracked
  handler/serve tasks, then use bounded asyncio.wait (for example2s) to drain them.
  Report a cleanup failure if anything remains. This forced failure cleanup never
  makes the test pass. A pytest outer bound of20s limits the whole experiment.

The old handler must fail the10s natural-completion gate with healthy fake readers;
the corrected handler must pass. Run this discriminating comparison in an explicitly
owned disposable checkout or via a temporary local reversal restored before commit;
never mutate the frozen release/shared worktree. Preserve actual failed/pass evidence.
If the installed platform cannot run this integration, stop and report the concrete
constraint for review; do not silently skip it or claim unit-test equivalence.

The integration establishes this WebSocket path's task draining, not systemd failed
state semantics or quiescence of every dashboard route. Root's separate rollout
review still owns those operational checks.

## Verification, commits and review

After both design approvals, write the failing direct regression first, implement
the small approved exception-boundary change, then run the full lifecycle suite
and the natural-shutdown falsifier. Run tests/test_dashboard_api.py and
tests/test_dashboard_cold_start_race.py with the lifecycle suite. Add only relevant
contract checks if the actual diff warrants them; no frontend rebuild for this change.

Run formatting and git diff --check. Commit the verified API/test slice and update
tasks/report_ws_disconnect_cleanup_2026_09_14.md with precise tests, versions,
natural-vs-forced completion evidence and limitations. Open a PR with available
codex/codex-automation labels. Two independent terminal PR approvals, folded findings
and exact final-head CI remain mandatory before root's authorized merge/integration.
No service restart or production request is part of this implementation task.

Current evidence is source/runtime analysis and approved plan only; no lifecycle
test or implementation has been written or run. A future revert restores the known
disconnect leak; document that consequence rather than treating revert as a fix.
