# “Now Tradable” panel shipping notes (read-only cockpit UI)

`BL-NEW-LIVE-DECISION-COCKPIT` shipped a read-only API endpoint: `GET /api/live_candidates`.

This runbook documents how to ship a **read-only** dashboard panel that renders those candidates.

## Hard constraints

- Panel must remain read-only (no buttons that trigger writes).
- Must include a visible disclaimer: “visibility-only; no live execution; TG/X context not ranked until price coverage is rankable”.
- Dashboard has write endpoints; do not add UI affordances that could be mistaken for enabling writes without explicit operator approval + auth/network verification.

## Why this is not built in the sandbox closeout run

- The dashboard serves committed `dashboard/frontend/dist/` assets.
- Shipping UI requires building and committing updated `dist/` artifacts (`npm ci && npm run build`) from a credentialed environment.

## Minimal implementation sketch

1. Add a panel component (pattern: `SourceCallsHealthPanel.jsx`) that fetches:
   - `GET /api/live_candidates?limit=...&window_hours=...`
2. Render buckets by server verdict (`candidate_review`, `watch`, `blocked`, `data_insufficient`) and show refusal reasons/caveats inline.
3. Wire the panel into an existing tab/page (prefer a “cockpit” or “health” tab; keep it discoverable).
4. Build + commit:
   - `cd dashboard/frontend && npm ci && npm run build`
5. Verify:
   - Run the contract checker against the running server: `scripts/check_live_candidates_contract.py` (outside sandbox if Python isn’t available).

