## Summary

- Add Actionability Gate v1 classifier for paper-trade cohort metadata.
- Add nullable `paper_trades.actionable`, `actionability_reason`, and `actionability_version` columns plus migration marker `bl_new_actionability_gate_v1`.
- Stamp actionability at paper-trade open with DB-side market-cap enrichment while preserving exploratory paper opens and live handoff behavior.

## Verification

```powershell
$env:PYTHONPATH=(Get-Location).Path
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest tests/test_actionability.py tests/test_paper_actionability.py tests/test_live_eligibility.py tests/test_trading_engine.py tests/test_trading_db_migration.py tests/live/test_paper_chokepoint.py --tb=short -q
```

Result: `84 passed, 1 skipped, 1 warning`

```powershell
$env:PYTHONPATH=(Get-Location).Path
$trading = Get-ChildItem -Path tests -Filter 'test_trading_*.py' | ForEach-Object { $_.FullName }
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest $trading tests/live/test_paper_chokepoint.py tests/test_live_eligibility.py tests/test_actionability.py tests/test_paper_actionability.py --tb=short -q
```

Result: `331 passed, 1 skipped, 1 warning`

Warning: existing `aiosqlite` event-loop-closed thread warning from `tests/test_trading_db_migration.py::test_post_migration_assertion_raises_on_incomplete_schema`.

## Scope Notes

- `would_be_live` remains live-slot eligibility.
- Actionability is metadata only; it does not suppress paper rows or change live handoff allowlist behavior.
- X/TG ranking, peak/no-peak exit handling, dashboard UI, and live-readiness policy changes are deferred.

## Post-Merge Follow-Ups

- Deploy and verify one fresh paper-trade open has `actionable`, `actionability_reason`, and `actionability_version`.
- Add dashboard/reporting for actionable vs exploratory counts, reasons, and filtering by actionable status.
- Run a 24-48h post-deploy comparison of actionable cohort PnL, exploratory cohort PnL, and false negatives among exploratory winners.
- Do not tighten live/paper entry suppression yet; collect classifier evidence first.
