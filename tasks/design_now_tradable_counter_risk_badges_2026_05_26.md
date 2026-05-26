# Design — Now Tradable counter-risk badges (BL-NEW-LIVE-DECISION-COCKPIT) — 2026-05-26

## Intent

Expose narrative/counter-risk context already present in `/api/live_candidates` directly in the Now Tradable cockpit table, without changing any backend behavior.

Read-only invariant: no writes, no execution, no ranking/scoring changes, and no use of TG/X context for ranking.

## Inputs (existing contract fields)

Per `LiveCandidateResponse`:

- `counter_risk_score: int | null` (0–100; display-only)
- `narrative_fit_score: int | null` (0–100)
- `counter_flags: list[dict|str]` where dict is expected to contain:
  - `flag: str`
  - `severity: "low"|"medium"|"high" | null` (optional)
  - `detail: str | null` (optional)

## UI placement (avoid widening the table)

Keep the existing table columns. Within the existing "Reasons" cell render:

Add one narrow `CR` column after `Verdict`:

- Cell shows `CR <score>` and a `+N` flags badge (tooltip contains full bounded details).
- Existing reasons text line (joined bullets) filters `counter_risk_present_display_only_v1` once CR is displayed (to reduce noise).

## Thresholds + colors (consistent across dashboard)

Standardize counter-risk score severity buckets:

- `low`: `< 30` (green-ish)
- `mid`: `30–60` (amber)
- `high`: `> 60` (red)

Fit score is informational only (no severity color beyond neutral text).

Flag chip color follows `severity` when present; otherwise neutral.

## Text + accessibility

- CR badge label is `CR <n>` (not "Risk") to stay compact.
- Add `title` tooltips:
  - CR: "Counter-risk (enrichment-only); does not change verdict."
  - Fit: "Narrative fit score (enrichment-only)."
  - Flag chip: show `flag` plus `detail` if present.
- Never inline JSON stringified dicts into the cell.

## Determinism / truncation

- Display order for tooltip flags is deterministic:
  - Convert each flag to a `{label,severity,detail}` shape
  - Sort by severity desc (critical > high > medium > low > info > unknown), then label asc
  - Tooltip shows first 20 segments, then appends an explicit “(+N more)” suffix

## Implementation plan (files)

- `dashboard/frontend/components/NowTradableTab.jsx`
  - Add helpers:
    - `riskBucket(score)` -> low/mid/high
    - `normalizeFlags(counter_flags)` -> normalized list
  - Render the CR column cell with bounded tooltip + basic a11y (`aria-label`, `tabIndex=0`).
- `dashboard/frontend/style.css`
  - Add CSS for `.risk-badge` + `.flag-badge` chips.
- `dashboard/frontend/dist/*`
  - Rebuild via `npm.cmd --prefix dashboard/frontend run build:codex` and commit artifacts.

## Verification

- Frontend build: `npm.cmd --prefix dashboard/frontend run build:codex`
- Manual smoke: open dashboard, navigate to "Now Tradable" and confirm:
  - table loads
  - CR/Fit/flags render when present
  - no layout blow-ups on long flag details (tooltips only)

## Rollback

Revert the single UI commit (JSX + CSS + dist). No DB migrations.
