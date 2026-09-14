# Successor health release — local source evidence

Source assembly commit4e6771679b55f2afef0c8b06bf48fb9e5c279aaa descends from deployed5c43526c46a33b06e7ed18509c8a0ea43c7772d3. It imports exactly the selected dashboard/tests blobs from merged master829d12b191ef2a1122dc18755492a5dd4fc106fc (PR585), including PR5841967cbfc42185fe4fb74d527bd0af9929375dae0. The final metadata-only commit is pinned by the external verified manifest, avoiding a self-referential SHA here. No full-master merge, push, runtime helper change, SQLite connection/copy or production action was performed by this assembly task.

## Reviewed scope and source proof

Both reviewers approved design f1a9250c and amendment ed680902 before implementation/assembly. The first manifest inspection discovered that the old1358-file protection set included TodayFocusPanel.jsx and TradeInboxTab.jsx, intentionally changed by merged PR584. Assembly stopped for the explicit two-file amendment; the manifest now retains all1358 original entries, preserves1356 byte/mode identities, and allows only the two reviewed original/replacement blobs at mode100644. An arbitrary newly selected protected file remains refused. The five operational dashboard.db ASTs retain d2/5c/final identity. Rollback remains the full5c tree, including original UI and release metadata.

The frozen five-PR manifest was independently regenerated from Git objects and compared in full before extending its selected set to575/577/578/580/583/584/585. The union contains52 paths; three historical removed assets were already absent from5c and require no new removal. Assembly changes24 dashboard/test paths. All staged entries outside explicit metadata matched expected Git blobs/modes before the source commit. The external successor manifest adds full runtime_baseline_files, original_core_files, exact core_ui_exceptions and candidate_metadata_files. Candidate metadata and expected_files together attest the complete candidate tree.

## Local verification

- Manifest TDD: initial13 expected failures with8 existing passes; the added UI-pin tests also failed before implementation. After implementation, a fixture-only frozen-dictionary alias was corrected. Final portable shallow-checkout fixtures:25 passed in84.35s. No external Git history is required by these fixtures.
- Combined existing viewers/core/API and new lane/health/controller/actual React suites:315 passed in45.50s, process exit0. There was one PytestUnhandledThreadExceptionWarning from an aiosqlite worker attempting to notify a closed event loop, attributed by pytest to the existing candidates API test, plus three installed Uvicorn/WebSocket adapter deprecations. This is Windows evidence; the thread warning is retained for Linux comparison and is not claimed resolved or attributed to a particular code change.
- Locked `npm ci --ignore-scripts` succeeded. Normal Vite build to a new private output directory transformed84 modules. All three generated files were compared byte-for-byte with both assembled files and merged829d Git blobs: index.html, assets/index-Cz-MDdDf.css and assets/index-iyKeNbDn.js. No package or lock file changed. Historical unreferenced assets remain only as specified by the manifest.
- `git diff --check` passed. The pinned copy validator remains SHA256a47b1aa3c28cdd7942a32e395053cab2e1a4b3b3efe983fd0793bbe2d13b8ac8. Ruff was unavailable in the existing local Python environment; no environment dependency was changed to add it.

Combined regression command (existing local Python environment):

```text
python -m pytest tests/test_stop_shortfall.py tests/test_stop_shortfall_summary.py tests/test_stop_shortfall_summary_frontend.py tests/test_postmortem_history_endpoint.py tests/test_postmortem_history_frontend.py tests/test_telegram_outcomes_endpoint.py tests/test_telegram_outcome_frontend.py tests/test_dashboard_nav_map.py tests/test_trade_surface_tg_alerts.py tests/test_dashboard_api.py tests/test_trading_dashboard.py tests/test_dashboard_cold_start_race.py tests/test_dashboard_hardening.py tests/test_dashboard_deep_link_bundle.py tests/test_dashboard_frontend_layout.py tests/test_check_dashboard_contracts.py tests/test_dashboard_websocket_lifecycle.py tests/test_signal_lane_status_endpoint.py tests/test_signal_lane_status_frontend.py tests/test_suppression_health.py tests/test_suppression_health_frontend.py -q --tb=short
python -m pytest tests/test_release_dashboard_harnesses.py -q --tb=short
```

## Canonical source SHA256

| File | Git blob content SHA256 |
|---|---|
| dashboard/api.py | 81cbba4c922293333397cc39bb23f536ffc7abc26255546da2801429a337e30b |
| dashboard/lane_status.py | 354aee9aea3ea643dbaa2a56e2b6b052058a9ac4e3fb8bb8851b4965c8067fe5 |
| dashboard/suppression_health.py | 0004d187920c40a7ebff98862f86aee341111afb310c34a987dd6401091234e5 |
| dashboard/frontend/signalLaneStatus.js | 95d174af1d833f823567e5aeb539735ff5ac9ba6e79d951ac41322270c1a2443 |
| dashboard/frontend/suppressionHealth.js | 6a46764049e7142052fc1b9e01353a5bdfaf92c3934409733736710f741db1b8 |

## Remaining gates

Root owns exact source archive/Linux regression/full initializer and content-copy proofs, archival/disk checks, two exact final candidate/helper reviews, source publication and fresh locked production preflight. Feature GitHub CI and this local source proof do not substitute for those gates. No deployment approval is implied by the local test results.
