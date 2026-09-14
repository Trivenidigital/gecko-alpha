**New primitives introduced:** one small read-only status endpoint and a shared
frontend status annotation/fetch helper, with a narrow lossless status reader.
Reuse existing read-only connection and badge/provenance patterns; no new policy,
ranking, state store or DB table. Existing status readers keep their behavior.

# DASH-08 current lane status annotation plan

## Hermes-first analysis

Drift was checked before ecosystem lookup. Existing functionality and the exact
residual are documented below; this is not a replacement Signal Trust surface.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko current lane status | none applicable identified in accessible directory; Hub catalog loading | Reuse read-only DB access; narrow lossless reader/API projection because existing reader coerces invalid values |
| React annotation and freshness | no Gecko-specific replacement identified | Existing React fetch, badge and provenance patterns; small shared presentation helper |

Checked2026-09-14: https://hermes-agent.nousresearch.com/docs/skills/ returned a
loading catalog; https://github.com/0xNyk/awesome-hermes-agent provides general
ecosystem capabilities. Verdict: reuse existing application mechanisms and add
only the missing annotation path. This is not an exhaustive negative catalog claim.

## Scope and approval boundary

PLAN ONLY, branch docs/dash08-lane-status-plan-20260914 in isolated worktree
C:/projects/gecko-alpha-dash08-20260914 from freshly fetched origin/master
d7a0e2672c8d67ba33dc47cfd1a818c2b2bad5b3. Read CLAUDE.md and relevant lessons.
Root explicitly requires two plan approvals, then separate design and two design
approvals before any implementation. Only this plan and tasks/todo.md change now.

Goal: a reader of Focus or Trade Inbox can see current recorded status for each
represented signal lane, including unavailable/stale evidence, without interpreting
a maturity label or a historical entry flag as current lane state.

No sorting, grouping, demotion, row filtering, card eligibility, dispatch, policy,
configuration, trade sizing or pipeline behavior changes. No new database tables,
background pipeline writers, secrets, external-service calls or runtime migrations.
The implementation will require ordinary reviewed frontend dist regeneration;
this planning task performs no build and authorizes no deployment.

## Drift evidence and data path

- Recovered untracked backlog_fable_analysis_2026_07_10.md:131 asks for lane badges
  and mentions demotion. It is dated requirements context, not current runtime
  evidence. Root narrowed this task to annotation only; demotion is excluded.
- dashboard/db.py:3620 already reads signal_params through _ro_db and returns
  enabled, suspended_at/reason and last_calibration_at keyed by signal_type.
  Missing schema/read errors propagate to the caller. However its int(enabled)
  coercion at3647 maps SQLite REAL1.5 to1, destroying evidence required to classify
  invalid data as unknown. Its output is therefore insufficient for the new
  annotation. A narrow lossless read of the same fields is justified; preserve
  this existing function and all its consumers unchanged.
- dashboard/api.py:412 joins that reader into the static Signal Trust registry,
  but only for registry entries and only when registry loading succeeds. This
  cannot provide independent complete lane status if the registry fails or omits
  a live lane. dashboard/api.py:2264's signal_params endpoint also computes rolling
  performance/audit and invokes cached ScoutDatabase initialization. Neither is
  the required lightweight read-only status projection.
- SignalTrustTab.jsx:13 has the badge pattern, but its predicate equates enabled0
  with SUSPENDED and treats other present values as enabled. Do not blindly copy
  that classifier for an annotation explicitly distinguishing disabled/unknown.
- dashboard/db.py:1607-1624 builds per-token surfaces from primary/recent paper
  signals; tracker presence adds top_gainers_tracker at1706. Inbox row exposes
  surfaces at1836; tracker-only rows expose that source at2036. Focus copies the
  same surfaces at2543. These are contextual source lanes, not one authoritative
  execution lane. Mixed rows need per-lane annotations, not a token-wide verdict.
- Inbox historical would_be_live at1838 is not current status. Focus and Inbox
  components have no current lane-status join. TodayFocusPanel.jsx:70 uses cached
  payload, and todayFocusStorage.js:3 sets a one-hour TTL. TradeInboxTab.jsx:201
  refreshes every30s when unpaused. Status freshness cannot inherit the hour cache.
- Operational code scout/trading/trade_surface_alerts.py:163-188 consumes existing
  Inbox/Focus db helpers. Preserve those helpers and their return values exactly;
  keep annotation in a separate read-only API/frontend presentation path.
- create_app at dashboard/api.py:215 declares _db_path global; legacy route reads
  can follow the last-created app. postmortem_db_path at220 demonstrates the
  explicit local capture pattern. The new route must capture its own path inside
  create_app at construction and never read the mutable global at request time.
- scout/trading/params.py:157 bypasses table params when SIGNAL_PARAMS_ENABLED is
  false and can fall back to settings on a missing row. engine.py:402 checks the
  resolved enabled value; further trust/execution gates exist. A table-status
  badge therefore must not promise an execution opportunity or claim every gate
  permits trading. Suspended_at metadata alone is not the engine's enabled check.

## Read-only runtime assumptions checked

At2026-09-14T02:36:26Z, bounded5s SQL probe used mode=ro against production scout.db,
read only the five required schema columns/status rows and aggregate recent counts,
and closed its connection. No app initializer/import or live API GET was invoked.
Selected environment-key inspection exposed no credentials. Local evidence:
C:/projects/dash08-readonly-probe.py and C:/projects/dash08-runtime-probe.txt.

- Deployed source remained d2f0d61edc63cb55ae159ec952cce404991f21f5; pipelinePID3032091.
- Required columns exist; signal_type is the primary key; duplicate-key count0.
  There are9 rows: narrative_prediction enabled1 with no suspension timestamp;
  the other8 enabled0. Six disabled rows have suspension timestamps; gainers_early
  and losers_contrarian have none. All last_calibration_at values are null.
- Current .env explicitly says SIGNAL_PARAMS_ENABLED=true; the pipeline process
  environment has no override for this key. These observations do not inspect
  the process's already-loaded settings/cache and are not a full execution verdict.
- No paper rows opened in the last36h. A natural live screenshot cannot currently
  prove the suspended-row presentation. Synthetic mixed-source fixtures are the
  primary acceptance evidence; no new trades or long calendar soak are required.

The runtime contradicts treating the backlog's narrative_prediction trust wording
as current suspension. Also, null/old calibration time is not a stale status read:
freshness must refer to observation time, not the age of policy changes.

## Proposed annotation contract for design

Add a small GET /api/signal_lane_status with an explicit app-local path captured
inside create_app, analogous to postmortem_db_path. A narrow read-only helper reads
the required fields without int/bool/string normalization of enabled. Validate the
raw SQLite type/value before classification or Pydantic coercion: fractional REAL
values such as1.5, invalid text, NULL and other noncanonical evidence must remain
unknown rather than become0/1. Design must specify strict accepted storage types
and JSON-safe unknown provenance without converting invalid values into valid ones.
Do not change get_signal_params_live or existing Signal Trust behavior as a shortcut.
Use no ScoutDatabase.initialize or Settings-driven engine evaluation. Return current
store evidence with an observation timestamp,
explicit source=signal_params, no-store caching, and visibility-only semantics.
Missing DB/schema/query errors produce explicit unavailable status, never an empty
success interpreted as enabled. Exact response/error schema and limits belong in
the separate design. No changes to existing Focus/Inbox response contracts required.

Frontend shares one annotation model/component across the two views, joining exact
surface names to the status response. Preserve row objects, identities, ordering,
counts, groups, actionability, canonical scores, dismissal/seen behavior and source
provenance. Unknown surface names and tracker-only provenance never fuzzy-map to a
different live lane. Display each lane independently; a mixed enabled/suspended row
must remain mixed. A details expansion can expose all lanes if space is limited,
but the collapsed annotation must disclose mixed/unknown status rather than choose
the most favorable lane.

Use precise labels with a nearby source explanation: current recorded lane status,
not a trade instruction or static trust tier. Expected classification:
enabled1/no suspension => enabled; enabled0/valid suspension timestamp => suspended;
enabled0/no suspension => disabled; missing row/invalid value/conflicting enabled1
plus suspension metadata => unknown with reason/raw provenance. Do not infer
suspension from last_calibration_at or lack of recent events. Design must specify
timestamp/value validation and how unexpected historical formats degrade.

Fetch status independently of Focus's cached rows. Proposed visible-view refresh30s,
freshness ceiling60s, with observation time shown; stale, failed refresh, offline,
unmounted and paused states must never continue claiming a current enabled badge.
Past values may be retained only as explicitly last-observed/stale details. Do not
persist status inside the existing hour-cached Focus payload. Cancel requests and
use generation ownership so late responses cannot restore older/current-looking
status after a newer refresh, failure, pause or tab switch. Exact refresh/pause and
clock-skew semantics must be settled by design and tested with controlled clocks.

This separate read avoids expanding exact Inbox/Focus field contracts or touching
operational consumers. If design finds an existing endpoint that fully satisfies
the independence/read-only/freshness contract, reuse it and remove the proposed
endpoint; partial registry matching alone is insufficient.

## Work sequence and meaningful falsifiers

- [ ] Two independent plan reviews: status semantics/data provenance and structural
  presentation/freshness scope. Fold findings and record exact approved SHA.
- [ ] Separate design pins endpoint schema, lifecycle/ownership/freshness behavior,
  exact join semantics and additive component placement; two design approvals.
- [ ] Test-first API isolation/missing-schema/error/empty-map cases with fixture DBs;
  create two app instances against distinct databases, then alternate/concurrently
  request each after the second construction: each must retain its captured path.
  Insert enabled=1.5 into a real SQLite fixture and assert unknown with preserved
  invalid evidence; int/bool/Pydantic coercion restoring enabled must fail the test.
  enabled0 versus suspension, enabled1, invalid/null/conflicting fields, mixed lanes,
  absent tracker key and stale/future observation cases with hand-derived outcomes.
- [ ] Prove presentation does not change ordered row keys, counts, groups, scores,
  dismissal/seen state or existing API payloads. Existing contract firewalls remain
  unchanged and pass; no production helper/schema mutation is allowed.
- [ ] Fake delayed responses: newer result followed by old response, refresh failure,
  pause/unmount, cached Focus rows and clock expiry. Old enabled status must not be
  resurrected or rendered current; status failure leaves all original cards usable.
- [ ] Implement only approved API/model/presentation files and focused tests. Likely
  files: dashboard/api.py, a narrowly scoped raw reader (separate module or additive
  helper chosen in design, existing reader unchanged), optional strict
  dashboard/models.py response model, shared
  frontend signalLaneStatus helper/component/hook, TodayFocusPanel/TradeInboxTab,
  tests/test_signal_lane_status_endpoint.py and frontend behavior tests.
- [ ] Run relevant API/Focus/Inbox/contract and frontend tests, reviewed build/dist
  parity (reuse DASH12 if merged), and a controlled mixed-source visual check.
  No production DB writes or live lane toggles are needed for validation.
- [ ] Commit verified slices and report evidence; PR, two independent code reviews,
  folded findings and exact-head CI before root's merge/deployment decisions.

Current checkpoint contains documentation only. Runtime figures are dated evidence,
not application constants. Refresh assumptions before eventual deployment; if table
semantics or source paths drift, re-evaluate the affected design instead of shipping
a misleading badge. Revert restores the prior absence of annotation, without policy
or persistent-state rollback.
