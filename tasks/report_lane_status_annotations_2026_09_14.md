# DASH-08 recorded lane annotation evidence

Plan a0703322 and design d0130893 each received two independent approvals before
build. Implementation checkpoint52ba7dd787ab4a5cee1273ee92f4451335b6eaca includes
lossless read-only status, generation-owned frontend observations and additive
Focus/Inbox annotations. Fresh master2b084e27 (PR582) was merged at a clean checkpoint.

## Behavior and boundaries

The new route captures its app-local DB filename. A worker owns the read-only SQLite
connection from open through close; strict stored INTEGER0/1 validation precedes
classification. REAL1.5 remains unknown with original evidence. Malformed/missing
schema, duplicate keys and read budgets fail unavailable. No initializer or DB
write is used. Existing status readers, card-producing helpers and policy remain
unchanged.

Each view owns a status controller independently of its card cache. Exact Map keys
support arbitrary lane names without prototype lookup. Mixed source lanes keep
separate enabled/suspended/disabled/unknown evidence. Pause, failure, stale clocks
and request ownership invalidate current-looking claims. Original cards, groups,
scores, order and dismissal/storage logic are unchanged. The view states that
recorded status is not execution eligibility; this is not a trust-tier classifier.

## Actual verification

- Red endpoint regression: HTTP404 before the route existed, then the same real
  SQLite test distinguishes REAL1.5 from INTEGER1 across two independently created
  apps. Backend23 tests passed, including acquisition/read cancellation barriers,
  closing the native connection after the awaiter cancels, bounded lock refusal,
  SQL interruption, schema absence and safe nonfinite evidence.
- Production controller test was red with the module absent, then passed delayed
  old-success/failure, pause/resume/dispose, timeout-late-success, malformed payload,
  future/rollback clocks, stale age and prototype-key cases. Actual React server
  render passed mixed annotations, stale primary text and unchanged board/input
  results. Render test requires existing npm dependencies; it explicitly skips in
  environments without them while the Node-only controller test still runs.
- Windows normal repository fixtures:146 focused API/frontend/Focus/Inbox/contract/
  existence tests passed. Formatting and git diff --check passed.
- Root authorized NEW exclusive0700 directory
  /root/gecko-dash08-validation-20260914. Bundle preserved exact52ba7dd7; no production
  or release source was modified. Locked npm ci succeeded using Node24.14.0 and
  npm11.9.0; Node distribution verified against published SHA256. uv sync used the
  existing frozen Python lock into this private checkout and private cache.
- Linux164 focused tests passed, including normal repository fixtures, actual React
  render and18 parity tests. Fresh Vite6.4.1 build transformed82 modules and matched
  all3 committed blobs at52ba7dd7: index.html, index-B06qoT95.css, index-BVatSAR6.js.
  Historical committed assets were retained. Lock/config/source dependencies did
  not change. Raw log: C:/projects/dash08-linux-proof.txt.
- Built-page headless Chrome with mocked APIs on owned localhost5191: Focus2rows,
  Inbox1row with both decision-card and table annotations, pause/failure preserve
  card count, zero page errors. Mobile390px Focus and desktop1280px Inbox screenshots
  inspected. Mixed fixture evidence is internally consistent and includes a missing
  tracker key. Browser never connected to production APIs.
  Script: C:/projects/dash08-visual.cjs. Images: dash08-focus-desktop.png,
  dash08-focus-mobile.png and dash08-inbox-desktop.png under C:/projects.
  An earlier repeat browser launch timed out before rendering; the final built-page
  run completed all assertions. A fixture locator originally matched hidden details
  too; corrected to the visible first chip. Neither was an application error.

## Remaining gate

WS PR583 merged as552db707b2f4698fba0c4f705100c2a641e066be and was integrated at
555f16769647ebadf60b279cc77ac27ac4dab452. Only tasks/todo.md conflicted; both task
sections were preserved. Actual API merge delta is exactly the inherited send
exception-boundary correction. Lane reader, frontend source/dist and lane tests
are byte-identical to the Linux/visual-verified checkpoint. Relevant lane/API/
cold-start/WebSocket tests76 passed, with3 existing dependency deprecation warnings.
No frontend reassembly was needed for this Python-only inherited change.

Root independently inspected the Focus-mobile and Inbox-desktop fixtures with no
visual finding. Inbox fixture omits unrelated metadata and is not evidence of
production metadata correctness. All browser assertions concern the new annotations
and preservation of fixture cards, not production data.

Open PR with codex/codex-automation labels. Two
independent code approvals and exact final-head CI remain mandatory. No merge or
deployment is performed by this task. Runtime evidence in the plan is dated and
does not claim any current engine process's in-memory eligibility state.
