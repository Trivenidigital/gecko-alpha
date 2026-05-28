**New primitives introduced:** `block_cause` factual classifier on `/api/todays_focus`; explicit Today's Focus research-link chips.

# Today's Focus Block Links Design

## Intent

Today's Focus should behave like a compact visibility panel over already-selected Trade Inbox rows. The trader should be able to answer three factual questions from one row:

1. What token is this?
2. Where do I inspect the chart/research page?
3. If it is blocked, is the block structural or policy/data-quality related?

This design does not decide whether to trade. It only reduces inspection friction.

## Data Contract

Add `block_cause: "data_path" | "data_quality" | "unknown" | null` to every Today's Focus row.

Rules:
- `null` for non-blocked rows.
- `data_path` for tracker-only or known plumbing/corpus/linkage reasons only
  when no immediate data-quality blocker is present.
- `data_quality` for price, timestamp, actionability, or data-insufficient
  blocks.
- `unknown` for blocked rows with no recognized reason.

The field is an enum, not prose. It is safe to render as `block=data_path` because it is factual classification, not trading guidance.

## Frontend

`TodayFocusPanel.jsx` adds local helper functions:
- `isContractAddress(tokenId)`
- `dexLink(row)`
- `coingeckoLink(row)`

Render chips beside the token identity:
- `Chart`: direct DexScreener route for recognized contract/chain rows.
- `Dex search`: DexScreener search fallback when no deterministic chart target exists.
- `CG`: direct CoinGecko coin page for slug rows.
- `CG search`: CoinGecko search fallback for contract rows.

The existing token title link stays in place. The new chips make the action visually obvious on mobile and desktop.

## Contract Firewall

`scripts/check_todays_focus_contract.py` adds `block_cause` to `EXPECTED_ROW_KEYS` and allowlists only `null`, `data_path`, `data_quality`, and `unknown`.

The forbidden language checks remain unchanged. The labels `Chart`, `Dex search`, `CG`, `CG search`, `block=data_path`, and `block=data_quality` are factual and do not use advice terms.

## Tests

Endpoint tests:
- tracker-only blocked row receives `block_cause="data_path"`;
- stale/not-actionable blocked paper row receives `block_cause="data_quality"`;
- normal non-blocked row receives `block_cause is None`.

Contract tests:
- clean payload with `block_cause=None` passes;
- unknown `block_cause` fails.

Frontend static tests:
- Today's Focus includes `researchLinks(row)`;
- panel renders `Chart` and `CG` chips;
- panel renders `block={row.block_cause}`;
- mobile CSS contains link-chip wrapping rules.
- `todayFocusLinks.js` table tests cover CoinGecko slugs, EVM contracts,
  Solana contracts, unsafe slug characters, unknown chains, and duplicate-symbol
  contract rows.

## Deployment Smoke

After merge/deploy, fetch `/api/todays_focus?window_hours=36` on srilu and print row count plus the distinct `block_cause` values. Per the Windows SSH rule, redirect SSH output to `.ssh_out.txt`, then read that file locally.
