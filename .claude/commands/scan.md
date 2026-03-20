Run one pipeline cycle in dry-run mode and print structured log summary.

```bash
uv run python -m scout.main --dry-run --cycles 1 2>&1 | tail -30
```

Summarise: how many candidates from each source, how many passed scoring, any errors.
