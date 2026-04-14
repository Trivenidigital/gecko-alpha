# Personalized Narrative Matching — Design Spec

**Date:** 2026-04-19
**Status:** Implementing
**Branch:** `feat/personalized-matching`

## Problem

Alert fatigue: the narrative agent alerts on EVERY heating category. Users only care about specific narratives (e.g., AI, DePIN, memes) and get overwhelmed by irrelevant alerts.

## Solution

Filter narrative alerts through user preferences stored as strategy keys in the existing `agent_strategy` table. No new tables required.

## Strategy Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `user_preferred_categories` | list[str] | `[]` | Category IDs to alert on in `preferred_only` mode |
| `user_excluded_categories` | list[str] | `[]` | Category IDs to never alert on in `exclude_only` mode |
| `user_min_market_cap` | float | `0` | Minimum token mcap for alerts (0 = no minimum) |
| `user_max_market_cap` | float | `0` | Maximum token mcap for alerts (0 = no limit) |
| `user_alert_mode` | str | `"all"` | `"all"` / `"preferred_only"` / `"exclude_only"` |

## Module: `scout/preferences/matcher.py`

Two pure functions:

- `should_alert_category(category_id, strategy)` — checks alert mode against preferred/excluded lists
- `should_alert_token(token_mcap, strategy)` — checks mcap bounds

## Integration Point

`scout/main.py` narrative alert loop — wrap the existing `if narrative_alert_enabled and prediction_models:` block with preference checks. Skip alert + log when preference filter rejects.

## API Endpoint

`GET /api/preferences/categories` — returns distinct category IDs from recent snapshots (last 24h) for preference selection UI.

## Files Changed

1. `scout/preferences/__init__.py` — empty package init
2. `scout/preferences/matcher.py` — matching logic
3. `scout/narrative/strategy.py` — add 5 new strategy defaults
4. `scout/main.py` — wire preference filtering into alert loop
5. `dashboard/api.py` — add `/api/preferences/categories` endpoint
6. `dashboard/db.py` — add `get_available_categories()` query
7. `tests/test_preferences_matcher.py` — unit tests

## Design Decisions

- **No new DB table** — preferences are strategy keys, editable via existing dashboard strategy editor
- **No new Settings fields** — preferences are runtime-tunable strategy, not env config
- **Pure functions** — matcher functions take strategy dict, no side effects, easy to test
- **Graceful default** — `user_alert_mode: "all"` means zero behavior change until user opts in
