# Review of Claude Code RH Pons increment

Reviewed tip: `b8843487`; product increment: `57af2e64`.
Review branch: `feat/claude-review-20260913`.

Verdict: the default-off protection is intact, but the collection and evidence
claims exceed the tested behavior. Fix the following before activation. This
review does not establish readiness of the rest of gecko-alpha for live trading.

## Findings

1. **P1 — empty block windows stall collection indefinitely.**
   `scout/ingestion/rh_pons.py:645-657` derives its next scan from the highest
   event row, not the last successfully scanned range. Two successful empty
   responses request `[90,1089]` twice. Later launches beyond that window are
   never discovered. `to_block` also lacks a chain-head bound. Persist completed
   scan progress independently of event density, and never advance past failed
   or partially processed work.

2. **P1 — polling cannot retrieve the curve trades the decoder supports.**
   `scout/ingestion/rh_pons.py:650-656` requests only the factory emitter.
   The declared trade decoder and synthetic fixture use each curve as emitter
   (`rh_pons.py:309`, `tests/test_rh_pons_collector.py:96`). Direct decoder tests
   bypass this transport exclusion. Collection must include verified curve
   emitters and revisit the launch block for newly discovered curves. This is
   a mismatch against the implementation's own event model, not a claim that
   the unverified onchain ABI has been validated.

3. **P1 — a crash between evidence and projection creates permanent loss.**
   `scout/db.py:865` commits the event before discovery/lifecycle projection;
   `scout/ingestion/rh_pons.py:486-488` skips projection on duplicate replay.
   The independent reviewer injected a discovery-write exception, restored the
   writer, and replayed the same launch: `duplicates=1`, `new_launches=0`,
   `discovery=None`. Make the writes atomic or replay projections idempotently.
   A scan checkpoint must not conceal partially processed blocks either.

4. **P2 — latency comparison credits observations on the wrong chain.**
   `scripts/compare_discovery_latency.py:99-111` joins by address alone despite
   available candidate `chain` and pool `network` fields. An Ethereum-only
   observation of the same address is credited to a Robinhood launch with
   `-86400` seconds latency and no censoring. Require chain-qualified identity;
   label unsupported identity mappings explicitly instead of guessing.

5. **P2 — removed events leave the lifecycle falsely canonical.**
   `scout/ingestion/rh_pons.py:391-411` only appends a marker;
   `scout/db.py:922-927` excludes markers when selecting a canonical hash.
   The independent reviewer reproduced launch -> graduation -> removed
   graduation: lifecycle stays `on_v4` and the removed hash remains canonical.
   Retain append-only evidence, but reconcile or invalidate its mutable
   projection. Polling also needs an actual canonicality-check path; accepting
   synthetic `removed` logs is not proof that polling detects reorganizations.

Additional pre-activation gaps: no block-header timestamp fetch in the polling
path, no freshness watchdog for these new tables, silent JSON-RPC error payloads,
and full RPC URL persistence at `rh_pons.py:663` (credential exposure if the
provider puts its key in that URL). These need bounded follow-up; no real RPC
credential was inspected or used in this review.

## Verification and limits

- Installed the isolated worktree's declared Python dependencies with system
  certificate trust (`uv --native-tls`); ordinary uv initially hit UnknownIssuer.
- `uv --native-tls run --extra dev pytest tests/test_rh_pons_collector.py
  tests/test_compare_discovery_latency.py --tb=short -q`: **24 passed, 10.54s**.
- `python investigation/reproduce_rh_pons_review_20260913.py`: exit 0; reproduced
  empty-range repetition and foreign-chain latency using mocks/in-memory SQLite.
  Its assertions describe existing defects, not desired regression contracts.
- Independent durable-evidence reviewer reproduced findings 3 and 5 using
  temporary databases. No production state was queried or changed.
- Claude Code CLI independently inspected the collection/latency paths and
  agreed with findings 1, 2, and 4. Its P0 labels were downgraded here because
  the registry and flag keep this path inert; there is no demonstrated outage.
- The author's full-suite baseline/flakiness claims were **not** reverified.
  Neither the original 24 tests nor this review is a live integration check.
- Branch contains an unrelated-history preservation merge. Do not merge this
  history wholesale into master. Integration needs a current-base diff review
  and squash/cherry-pick strategy; the original branch was not modified.

## Direct coordination

Installed Claude Code 2.1.270 was already authenticated. No plugin was needed.
`claude agents --json` located an idle gecko-alpha session, but `--resume` could
not find its reported ID. Consequently the completed review came from a fresh
Claude Code session with repository context, not original-author conversation
memory. Session: `3b61201c-6b71-4273-ba91-31c0be4a7b1d`.
The follow-up successfully resumed that same session (`is_error=false`); Claude
acknowledged the reproduced findings and accepted the P1/P2 severity correction.

See `docs/runbooks/codex-claude-coordination.md` for the verified CLI workflow.
Local JSON responses are retained in this worktree but excluded from this
review commit; they include runtime metadata and are not source artifacts.

## Next bounded work

First correct scan progress, emitter coverage, and crash-safe projection
replay with adversarial regression tests. Then correct chain-qualified latency,
canonicality, timestamp capture, and monitoring. Before any activation,
verify the real deployment/ABI/chain, active runtime configuration and expected
event rate. Live execution remains a separate readiness decision; the unresolved
quote-asset approval and execution coverage are not answered by this collector.
