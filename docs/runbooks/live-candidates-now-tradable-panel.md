# "Now Tradable" panel notes (read-only cockpit UI)

`BL-NEW-LIVE-DECISION-COCKPIT` provides a read-only API endpoint: `GET /api/live_candidates`.

The dashboard includes a **read-only** "Now Tradable (V1)" UI tab that renders those candidates.

Repo truth: the tab is merged in-tree via PR #239 (merge commit `050fe12b`). This does **not** assert production deployment state.

## Hard constraints

- Panel must remain read-only (no buttons that trigger writes).
- Must include a visible disclaimer: "visibility-only; no live execution; TG/X context not ranked until price coverage is rankable".
- Dashboard has write endpoints; do not add UI affordances that could be mistaken for enabling writes without explicit operator approval + auth/network verification.

## Modify the UI (requires rebuilding dist)

- The dashboard serves committed `dashboard/frontend/dist/` assets.
- Shipping UI requires building and committing updated `dist/` artifacts (`npm ci && npm run build`) from a credentialed environment.

## Verification (operator workflow)

1. Verify the API contract against the running server:
   - `python scripts/check_live_candidates_contract.py`
2. Verify the UI renders buckets and disclaimers in a browser session (read-only).
