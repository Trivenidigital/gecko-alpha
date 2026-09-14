# Selective release candidate evidence

Assembled locally on 2026-09-14 after all four feature PRs merged. No push,
production changes, service actions or remote actions performed by this agent.

Production baseline: d2f0d61edc63cb55ae159ec952cce404991f21f5.
Final feature master: d7a0e2672c8d67ba33dc47cfd1a818c2b2bad5b3.
Selected squash commits: 575 d5b26ff791ff64dac65d77d2a10a841df1c34007;
577 7cbd82dd962ba3e4ce4ad53be1f7019f71e08307;
578 f55271fcff9a1db19884c2cb5e7a97e032e4b920;
580 d7a0e2672c8d67ba33dc47cfd1a818c2b2bad5b3.

Restored 28 dashboard/test paths from final master. The selected union also names
three intermediate asset paths absent from both final master and production; these
remain absent. Baseline assets outside that union remain byte-identical, even when
unused. Runtime core, configuration, dependencies and all other nonallowlist paths
remain production-identical. Release metadata and disposable harnesses are explicitly
listed in the external verified manifest; its release_sha identifies this candidate.

## Local verification

Using C:/projects/gecko-alpha/.venv/Scripts/python.exe:

```text
-m pytest tests/test_stop_shortfall.py tests/test_stop_shortfall_summary.py tests/test_stop_shortfall_summary_frontend.py tests/test_postmortem_history_endpoint.py tests/test_postmortem_history_frontend.py tests/test_telegram_outcomes_endpoint.py tests/test_telegram_outcome_frontend.py tests/test_dashboard_nav_map.py tests/test_trade_surface_tg_alerts.py tests/test_release_dashboard_harnesses.py -q
```

160 passed, one aiosqlite worker teardown warning. Isolated strict-warning runs:
summary suite20 passed, trade-surface suite14 assertions pass but exits1 with
PytestUnhandledThreadExceptionWarning: Event loop is closed. Unchanged test
test_send_trade_surface_alerts_paces_multiple_send_attempts at lines438-485 opens
a Database and omits close; preceding async tests explicitly close theirs. Both
the entire test file and scout/trading/trade_surface_alerts.py match production.
No feature-source edits or warning suppression made. Linux validation must record
the actual result and reviewer disposition of this existing test cleanup limitation.

```text
-m pytest tests/test_dashboard_api.py tests/test_trading_dashboard.py tests/test_dashboard_cold_start_race.py tests/test_dashboard_hardening.py tests/test_dashboard_deep_link_bundle.py tests/test_dashboard_frontend_layout.py tests/test_check_dashboard_contracts.py -q
```

109 passed. Harness-only suite8 passed, including memory bounds and trigger writes.

`npm run build:codex -- --outDir <automationdir>/release-build-parity` transformed
80 modules using existing sibling node_modules through an ignored local junction.
Binary comparison of all three emitted files against final-master Git blobs passed:
index.html, assets/index-CqyQY5c1.css, assets/index-Dtb0JCcH.js. The checked-in
production-baseline bundle was never overwritten by this build.

## Remaining evidence

Root owns clean Git exports, exact-candidate Linux tests with runtime package parity,
private production backup/copy app and policy validation, pinned response-content
checks, refreshed runtime assumptions and two independent candidate reviews.
This report is Windows local evidence, not Linux validation or GitHub CI.
Ops-requested memory fold is separate commit ac2e13aa8d08528a9e6360e463a4e7051319e70e.
Deployment remains pending those approved-design checks; merged and deployed are
different states.
