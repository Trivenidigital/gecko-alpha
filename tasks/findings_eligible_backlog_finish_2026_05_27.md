# Eligible Backlog Finish Findings - 2026-05-27

## Scope

This pass reviewed currently visible backlog/todo items after PR #294 and separated:

- already shipped but stale in task text,
- still gated by pre-pinned data/operator conditions,
- re-scope-eligible but not build-ready,
- blocked because the current data model lacks a safe target.

No runtime behavior change is made by this findings PR.

## Current GitHub State

`gh pr list --state all --limit 20` showed:

| PR | State | Decision |
|---|---|---|
| #289 Signal Trust scorecards | MERGED 2026-05-26 | shipped; stale todo/backlog text can close |
| #290 Trade Inbox counter-risk | MERGED 2026-05-26 | shipped; stale todo text can close |
| #278 Now Tradable counter-risk badges | CLOSED, unmerged | superseded by PR #290 on Trade Inbox |
| #280 TG alert parking docs | CLOSED, unmerged | superseded by current backlog/lessons state |
| #33 paper-trade edge detection | CLOSED, unmerged 2026-05-22 | stale open/design-review backlog text must be corrected |

No open PRs existed at the start of this pass.

Prod smoke for shipped dashboard work:

- srilu prod HEAD was `2e8cf69` during smoke, which includes PR #289 and PR #290.
- `GET /api/signal_trust/scorecards` returned HTTP 200 and read-only metadata including `not_for_alerting=true`.
- `GET /api/trade_inbox?limit_per_group=2` returned HTTP 200 and sampled rows include `counter_risk_score`, `counter_flags`, and `counter_risk_predicted_at`.

## Runtime Evidence

### Tracker-Promotion Soak

Command:

```bash
cd /root/gecko-alpha
sqlite3 scout.db < scripts/trade_inbox_tracker_promotion_soak.sql
```

Output:

```text
2026-05-26|17
2026-05-25|50
```

Decision: `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN` remains `GATED / SOAK-METRIC-NOT-YET-AUDITABLE`. The pre-pinned gate is `>= 5` unique tracker-promoted `coin_id`s/day for `>= 3` mature UTC days, or the 14-day backstop, but the current SQL scans only `datetime('now', '-36 hours')`; a single run cannot prove three mature UTC days. It also suppresses against current open paper rows, so historical counts can drift as paper trades open/close. Unlock requires either a widened/fixed SQL query covering at least four UTC days with point-in-time paper state, or three recorded daily artifacts with `run_at`, SQL hash, rows, and the current-open-paper caveat. Backstop: 2026-06-09.

### Held-Position Stale Count

Command shape:

```bash
journalctl -u gecko-pipeline --since '7 days ago' --no-pager
```

Parser: JSON lines filtered to `event == "held_position_refresh_summary"`.
Threshold semantics checked as strict `stale_open_count > max(5, 0.05 * held_total)`, not `>=`.

Parsed `held_position_refresh_summary` JSON:

| Metric | Value |
|---|---|
| summary rows | 3,878 |
| range | 2026-05-20T01:42:52Z through 2026-05-27T01:39:12Z |
| `stale_open_count` min/p50/max | 2 / 4 / 5 |
| `held_total` min/p50/max | 125 / 139 / 150 |
| `not_found_count` min/p50/max | 0 / 0 / 146 |
| `refreshed_count` min/p50/max | 0 / 131 / 146 |
| CoinGecko lane-backoff events | 3,188 |
| cycles exceeding `max(5, 0.05 * held_total)` | 0 |
| max consecutive exceed cycles | 0 |

Decision: do not implement `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT` now. Keep it open as `BASELINE-MEASURED / BELOW-SUGGESTED-THRESHOLD / OPERATOR-THRESHOLD-PENDING`; reopen implementation only if the operator chooses a lower threshold or future baseline breaches the chosen threshold.

### Narrative Deferred Resolution

Prod SQLite checks:

| Metric | Value |
|---|---|
| unresolved CA rows, last 7d | 24 |
| unresolved CA rows, all-time | 39 |
| resolved narrative rows, all-time | 0 |
| candidate matches, last 7d | 3 rows |
| distinct matched CA count | 1 |

The three candidate matches are all the same Solana CA:

```text
5hiLgyybrAYPpUwNFa38agfZ8iEtnahWKAPixcfspump
```

Current `candidates` schema has no `coingecko_id`; `/api/coin/lookup` returns `coin_id=None` for `candidates` hits. A table audit found `coingecko_id` only on `second_wave_candidates`, not on `candidates` or narrative rows.

Hermes runtime:

```text
gecko-x-narrative-scanner enabled=true last_status=ok last_run_at=2026-05-27T01:00:52.525883+00:00
```

Current resolver contract: `scout/api/narrative_resolver.py` returns `"coin_id": None` for `candidates` hits. The relevant Hermes `jobs.json` fields were `enabled=true`, `last_status=ok`, `last_error=null`, and `last_run_at=2026-05-27T01:00:52.525883+00:00`.

Decision: do not write `resolved_coin_id` with contract address, ticker, or any other surrogate. Keep `BL-NEW-HERMES-NARRATIVE-DEFERRED-RESOLUTION-SWEEP` blocked as `BLOCKED-CANONICAL-ID / SOURCE-CALL-IDENTITY-RESOLUTION` on a canonical CA-to-CoinGecko-id resolver or the broader source-call identity-resolution work.

## Branch Decisions

| Item | Decision |
|---|---|
| Signal Trust scorecards | SHIPPED via PR #289; stale active-work tail closed |
| Trade Inbox counter-risk | SHIPPED via PR #290; PR #278 superseded |
| PR #33 paper-trade edge detection | CLOSED-SUPERSEDED; stale open/design-review backlog text corrected |
| PR #280 TG parking docs | CLOSED-SUPERSEDED; stale "close rather than merge" text corrected |
| TG alert qualification | GATED / SOAK-METRIC-NOT-YET-AUDITABLE; needs widened/fixed SQL or recorded daily artifacts |
| Held-position stale alert | BASELINE-MEASURED / BELOW-SUGGESTED-THRESHOLD / OPERATOR-THRESHOLD-PENDING; no alert now |
| Hermes narrative deferred resolution | BLOCKED-CANONICAL-ID / SOURCE-CALL-IDENTITY-RESOLUTION; no unsafe surrogate writeback |
| X/TG outcome linkage and no-peak risk | RE-SCOPE-ELIGIBLE; not build-ready without current-base triage and per-item runtime gates |

## Anti-Scope

This PR does not change Telegram alerting, urgency tiers, ranking, source pruning, signal parameters, auto-suspend logic, live execution, sizing, paper-trade policy, cron schedules, DB schema, or paid/vendor calls.
