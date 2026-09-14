# Historical postmortem viewer verification

2026-09-13: bounded list-only viewer; no production writes or deployment.

## Reviews and folds

Two plan reviews approved final527ce25c. Structural review required decimal-string
IDs/cursors and newest-ID capture timestamp; operations required explicit paper
entry-to-cached-capture price semantics. Two design reviews approved final
e7b5cc28: anomalous text becomes unavailable with field reason, never silent
truncation. Source build also bounds UTF-8 bytes because SQLite length(text)
stops at embedded NUL; a failing regression demonstrated the bypass before fix.

## Verification

Initial endpoint tests failed404 before implementation. Frontend executable Node
test failed missing helper before implementation. All final tests passed:

C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest tests/test_postmortem_history_endpoint.py tests/test_postmortem_history_frontend.py tests/test_dashboard_nav_map.py tests/test_dashboard_api.py tests/test_stop_shortfall.py -q --tb=short

Initial result:141passed; after per-app database isolation correction,142passed. Tests cover read-only preservation/SQLite write refusal, no
evidence-column access via SQLite authorizer, finite numeric/null serialization,
malformed text sanitized503, missing DB/schema vs empty history,64-bit cursors,
intervening inserts, latest timestamp from newest ID, bounded anomalous fields,
and actual frontend request/paging controller stale success/error/finally races.

npm --prefix dashboard/frontend run build passed (Vite6.4.1,76modules).
Black checks passed for touched Python files; git diff --cached --check passed
on merged source after whitespace cleanup. Branch merged current origin/master
with PR575 d5b26ff; generated asset rename conflicts resolved by clean rebuild.

Root independently verified actual component in a standalone synthetic fixture:
readable headers/rows/caveats; Next25-to6 rows/Page2/Next disabled; empty mode shows
zero/no captures; unavailable mode shows Retry without zero/table. This proves
component rendering/pagination, not deployed API or full-app health. Local QA
fixture/server files are not included in PR.

## Release boundary

Both reviewers initially approved4131d358. Root then found the module-global DB path could redirect a prior app to a subsequently created app database. A failing two-app regression confirmed the issue; route-local capture fixed it in2cb1d4f96d9e96461df11146194257f25df5dd63. Both reviewers renewed approval on that exact code head: stop_gap_audit logic/concurrency (17 independently rerun tests), stale_pr_audit ops-safety/silent-failure (14 independently rerun tests). No outstanding fixes. Full related focused suite:142passed. Clearances recorded in .reviewers/577.toml. Awaiting exact final-head CI. No deployment while separately
owned capture/migration work is active. Later rollback is revert viewer commit
and rebuild prior frontend; no DB restore required. Historical list visibility
is complete; comprehensive missed-token capture, causal attribution, guaranteed
T-minus evidence and writer freshness watchdog remain separate residuals.

## Master integration 2026-09-14

Integrated origin/master e6a55d7a (PR576) into prior PR577 head2e32df72.
Only conflict was tasks/todo.md; retained both independent checklist sections.
No custom code changed. Dashboard source and generated assets were preserved;
a fresh frontend build reproduced the committed bundle with no diff.

Fresh verification: the exact five-file pytest command above passed142tests;
npm --prefix dashboard/frontend run build passed76modules. Black --check on
dashboard/api.py, dashboard/db.py, dashboard/models.py and the three postmortem/
navigation test files passed unchanged (Python3.12/3.14 target warning emitted).
The PR delta passes git diff --cached origin/master --check. The merge delta
against the old feature head reports only two pre-existing blank-at-EOF warnings
imported unchanged from PR576 (.reviewers/576.toml and capacity smoke JSON).

Existing clearances have not been repointed: the inherited scout/scripts/tests
changes require renewed independent review of the integrated candidate and
fresh exact-head CI. Root coordinates those gates. No merge or deploy performed.