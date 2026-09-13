# Deliverable report — RH/Pons discovery increment (2026-09-13)

Session: `claude/main-session-last-discussion-b8hsn7` (session-owned worktree;
root checkout left on master). Companion docs:
`tasks/design_rh_pons_discovery_delta_2026_09_13.md` (design delta + New
primitives declaration) and
`tasks/findings_chain_coverage_matrix_2026_09_13.md` (coverage matrix,
suppression boundaries, safety trace).

## Approvals log

| Action | Class | Approval record | Timestamp |
|---|---|---|---|
| Coverage matrix + design delta docs | implementation (docs) | Operator ruling message 2026-09-13 ("This message records my approval for the bounded implementation below: the coverage matrix, additive inert RH Pons collector, evidence integration, latency comparison harness, and focused tests") | 2026-09-13 |
| Inert RH Pons collector + evidence tables (migration 20260913) | implementation (additive, observe-only) | same ruling | 2026-09-13 |
| Latency comparison harness + focused tests | implementation | same ruling | 2026-09-13 |
| Commit + push to `claude/main-session-last-discussion-b8hsn7` | branch push (no merge, no deploy) | same ruling ("Complete authorized work without another general confirmation request"); designated-branch instruction of record | 2026-09-13 |
| Production deployment / activation / external alerts / signing / funding / live trading | — | **NOT AUTHORIZED — none performed.** `RH_PONS_COLLECTOR_ENABLED` defaults False; no deployment is `onchain_verified`; no RPC URL is configured | — |

No PR was opened (not requested). No flag or prod-state change was made.

**Branch-collision record (2026-09-13).** While this increment was in test,
the designated branch was force-updated by a parallel session to an
UNRELATED clone lineage (776 commits, tip `6ac3a08`, 2026-07-22
investigation work; no common ancestor with this repo's master line).
Resolution chosen to lose nothing and force nothing: (1) `-s ours` merge
(`7b859b0`) records that lineage as an ancestor with the tree unchanged, so
every parallel commit stays reachable from the branch; (2) restore commit
(`12dd995`) brings its unique files into the tree —
`investigation/ruling_response_queries.sh`,
`investigation/time_death_counterfactual.py`,
`tasks/operator_pack_p5_and_dex_activation_2026_07_20.md`, and its two test
files preserved verbatim under
`investigation/parallel_branch_tests_20260722/` (one asserts July-era
schema shapes and fails on the current line — cross-lineage
incompatibility, so they are kept out of pytest collection until rebased).
Differing versions of shared paths remain recoverable via
`git show 6ac3a08:<path>`. NOTE for any future PR of this branch into
master: use a SQUASH merge, or the unrelated 776-commit lineage will enter
master's history.

## Implemented changes

1. **Coverage matrix** — `tasks/findings_chain_coverage_matrix_2026_09_13.md`:
   discovery/identity/pricing/safety/quoting/simulation/reconciliation ×
   {Robinhood, Base, BNB, Ethereum}, every cell cited or explicit-unknown.
   Notable corrections it records: the 0x AllowanceHolder allowlist covers
   **Ethereum mainnet only** (Base execution is not currently a verified
   reference); GoPlus map lacks BNB despite its comment; the counter's $15k
   threshold is advisory-only.
2. **Inert RH Pons collector** — `scout/ingestion/rh_pons.py`: versioned
   deployment registry (conflicting factory addresses BOTH recorded, all
   `source_derived_unverified`, none collectable), runtime-derived event
   topics (keccak verified against the universal Transfer vector),
   transport-free `collect_from_logs` core, forward-only
   `on_curve → graduating → on_v4` (+`rescued`/`unknown`) projection,
   raw-preserving handling for unresolved layouts (`LaunchSwept` etc.),
   reorg marker rows, and a triple-gated `poll_once` (flag AND RPC URL AND
   onchain-verified deployment — all three false today).
3. **Evidence integration** — `scout/db.py` migration `rh_pons_discovery_v1`
   (schema_version 20260913): `curve_launch_discoveries` (projection, two
   clocks, eligibility stamp) + `curve_launch_events` (append-only, reorg
   markers, raw payloads). Bare-additive; observe-only.
4. **Latency harness** — `scripts/compare_discovery_latency.py`: read-only,
   explicit censoring (`event_time_unavailable`, `never_observed_cg_ds_gt`,
   `never_observed_dex_lane`), provider availability reported or "unknown",
   JSON output.
5. **Eligibility contract (ruling 4)** — `execution_eligibility()` in
   `rh_pons.py`: unknown ⇒ ineligible; reuses `is_safe_strict` semantics;
   verified against outage / timeout / missing-record / adverse-verdict via
   aioresponses. The execution-path trace result is in the findings doc §2:
   `scout/live/**` calls no GoPlus function; the fail-open wrapper is
   alert-path-only (`scout/main.py:1344`); no legacy migration performed.
6. **Wiring** — `scout/main.py`: flag-gated call after the GT discovery lane;
   flag off (default) ⇒ byte-identical pipeline. `scout/config.py`: four
   `RH_PONS_*` settings, no default RPC endpoint on purpose.

## Verification evidence

- Focused suites `tests/test_rh_pons_collector.py` (23 tests) +
  `tests/test_compare_discovery_latency.py` (1 test): **24/24 passed**,
  covering: topic-derivation keccak vector, registry inertness, migration,
  TokenLaunched decode + eligibility stamp, first-buy-in-launch-transaction
  ordering (reversed input), duplicate dedup, unknown-topic raw preservation,
  non-registry-address refusal, reorg removed/replaced markers (append-only
  proven), forward-only lifecycle incl. graduation-without-seen-birth and
  backfill NULL-fill with earliest-first-seen retention, resume cursor,
  poll_once triple-gating (no HTTP under refusal), transport + reconnect
  resume (fromBlock advances past observed head), eligibility mapping, and
  the four strict-safety cases.
- Full suite (this branch): 6808 passed / 18 skipped / 40 failed. Baseline
  full suite on UNTOUCHED master in the same container: 39 failed — the
  identical 39 environment-dependent tests (watchdog/backup shell-script
  suites etc.). The single branch-only failure
  (`test_dex_discovery_watchdog_script.py::test_future_corrupted_cooldown_cannot_suppress_breach`,
  a wall-clock cooldown test on a script this increment does not touch)
  passes in isolation on this branch — flake under double-suite CPU load,
  not a regression. Net: zero failures attributable to this increment;
  scaffold tests unmodified. (`docs/migration_versions.md` gained the
  required 20260913 allocation row — its absence was this increment's one
  genuine full-suite failure, fixed.)
- Formatting note: `uv run black` (unpinned, latest) reformats ~95
  PRE-EXISTING files in this repo — version drift, not this increment's
  churn. All such churn was reverted; only this increment's files were
  touched. Recommend pinning black in dev deps (not done here — out of
  scope).

## Fixture provenance disclosure

All test fixtures are SYNTHETIC encodings of the source-derived layouts
(`fixture:source_derived`). They are **source-verified at best, not
onchain-verified**, and none of this constitutes a successful live
integration check. The `poll_once` transport test marks a TEST-ONLY registry
entry as verified via monkeypatch; the shipped registry contains no
collectable deployment.

## Exact unresolved dependencies

1. First-party confirmation of Pons V2 factory / router / PoolManager / hook
   addresses + deploy blocks (docs.ponsfamily.com, docs.robinhood.com,
   docs.bitquery.io and `rpc.mainnet.chain.robinhood.com` are all
   egress-blocked from this environment).
2. Onchain verification: `eth_getCode` on registry addresses; one real
   `TokenLaunched` log confirming derived topic0; block timestamps for
   `event_time`.
3. Factory-address conflict resolution: `0x7eD5…` (pons-mcp) vs `0xA5aA…`
   (Bitquery-derived "active"), plus phase-numbering and snipe-window (3s vs
   5s) discrepancies.
4. `LaunchSwept` / `GraduationTokensPermanentlyLocked` layouts and the RH v4
   fork's `Initialize` layout — until then `graduating` is not directly
   observable and unknown factory logs are preserved raw.
5. `x1L` definition + the operator-approved quote-asset allowlist (ruling 1:
   treated as unresolved; nothing invented).
6. Provider-availability clocks for CG/DS/GT (harness reports "unknown"
   pending an availability-capture seam).
