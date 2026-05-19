## Summary

- Expose paper-trade actionability metadata in Trading dashboard API rows.
- Add server-side closed-trade `actionability=all|actionable|exploratory|unknown` filtering with matching count semantics.
- Add `/api/trading/actionability` rollup for open counts, closed PnL cohorts, and top reasons.
- Add Trading tab actionability summary, closed-trade filter, and per-row actionability badge/reason display.
- Add post-deploy validation runbook for fresh stamp checks, 24h cohort PnL, and exploratory false-negative winners.

## Verification

```powershell
$env:PYTHONPATH=(Get-Location).Path
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest tests/test_trading_dashboard.py tests/test_actionability.py tests/test_paper_actionability.py --tb=short -q
```

Result: `46 passed`

```powershell
npm run build
```

Result: Vite build passed, emitted `dist/assets/index-Ca4N1ClP.js`.

```powershell
git diff --check
```

Result: clean.

Local smoke: temp-DB dashboard server returned HTTP 200 for `/` and `/api/trading/actionability`.

## Scope Notes

- Actionability remains metadata only.
- No suppression of exploratory paper trades.
- No live-entry or capital-allocation policy changes.
- Fresh post-deploy row stamp verification remains in the runbook because no paper trade opened immediately after #181 deployment.
