# Gecko-Alpha — Operational Hygiene & Conventions

**Project:** gecko-alpha (Python async paper-trading pipeline)
**Status:** v1 — captures patterns deployed as of 2026-05-03
**Audience:** anyone proposing schema, test, or architecture work — including future Claude/Copilot/human contributors

**New primitives introduced:** (this doc IS the primitive being introduced)

---

## How to read this document

Four parts:

1. **Deployed patterns** — what `scout/` and `dashboard/` actually do today. Descriptive, not prescriptive.
2. **Operational drift checklist** — silent-failure surfaces ranked by stakes, with realistic costs. Every entry has owner + due-date OR explicit `deferred-indefinitely (reason)`. NO bare TODOs.
3. **Working agreement** — the structural fix for "drift via imported priors": read deployed code before proposing. Plus the new-primitives-declaration convention enforced by `.claude/hooks/check-new-primitives.py`.
4. **What this doc is NOT** — explicit limits so the doc doesn't creep toward doctrine.

This is a working document, not a constitution. When deployed patterns change, update Part 1. When the checklist gets resolved or accumulates new items, update Part 2. The drift-check workflow in Part 3 is a habit + a hook, not a process gate.

---

## Part 1 — Deployed patterns

What `scout/`, `dashboard/`, and `tests/` provide today and how new code is expected to use it.

### Async everywhere

`aiohttp.ClientSession` for HTTP, `aiosqlite` for DB, `asyncio.gather()` for parallelism. NEVER use `requests`. Pass `session` as a parameter; no global session. See `scout/main.py` for the orchestration entry; every ingestion module (`scout/ingestion/coingecko.py`, etc.) takes `session` as an argument.

### Pydantic v2 settings

`scout/config.py:Settings` extends `BaseSettings` reading `.env`. Field validators (e.g. `@field_validator("PAPER_SL_PCT")` at line 419) enforce cross-field constraints. NEVER use `os.getenv()` in business logic — always go through `settings`. Settings are passed in as parameters, not imported as a global.

### Structured logging

`structlog.get_logger()` everywhere. Every event has `event="some_name"` plus structured fields. NEVER `print()`. Search prod logs by `journalctl ... | grep event=trade_skipped_signal_disabled` style.

### DB layer

`scout/db.py:Database` is a thin async wrapper around aiosqlite. `initialize()` opens the connection, sets `PRAGMA journal_mode=WAL` (line 67) and `PRAGMA foreign_keys=ON` (line 70), then sequentially calls `_create_tables` + each `_migrate_*` method.

### Migration pattern

`BEGIN EXCLUSIVE` + per-statement `await conn.execute(stmt)` + explicit `await conn.commit()` / `ROLLBACK`. NEVER `executescript` **in migration methods** (implicit COMMIT defeats rollback semantics — see `scout/db.py:1139-1144` comment). The single exception is `_create_tables` (`scout/db.py:90`, with `executescript` call at line 93) which uses it legitimately for initial schema creation — no prior rows exist to lose, and the implicit COMMIT is acceptable for the bootstrap path.

Indexes for added columns live in the migration step, NOT `_create_tables` (per `feedback_ddl_before_alter.md` memory). `CREATE TABLE IF NOT EXISTS` is a no-op for existing tables, so any paired index declaration there silently skips on the upgrade path.

### Datetime storage

Write Python `datetime.now(timezone.utc).isoformat()`. Read & compare via `datetime(stored_col)` SQL wrapper for `T` vs space format normalization (per PR #24 audit). Examples in `scout/trading/engine.py`, `scout/trading/auto_suspend.py`, `scout/trading/calibrate.py`. SQL `datetime('now')` defaults on column DDL are acceptable for INSERTs that omit the column; explicit Python ISO timestamps for all UPDATEs.

### Tier 1a per-signal params

`scout/trading/params.py:get_params(db, signal_type, settings)` returns a `SignalParams` dataclass. Read precedence:
1. `SIGNAL_PARAMS_ENABLED=False` → always Settings (`source='settings'`)
2. `signal_type in DEFAULT_SIGNAL_TYPES` AND row exists → table (`source='table'`)
3. Known signal_type AND row missing → log error, return Settings
4. Unknown signal_type → raise `UnknownSignalType` (typo guard)

Cache TTL 5 minutes, version-bumped on `bump_cache_version()` after `--apply`/`auto_suspend` writes. The lenient variant `params_for_signal()` exists for the evaluator hot path (history rows may have legacy types like `momentum_7d` or `long_hold` that aren't in `DEFAULT_SIGNAL_TYPES`).

### Signal-dispatch kill switches (3 layers)

| Layer | Where | Affects |
|---|---|---|
| `.env` `PAPER_SIGNAL_*_ENABLED` (outer, no DB) | `scout/main.py:461` (losers), `scout/narrative/agent.py:135` (trending) | When False, dispatcher never calls `engine.open_trade` |
| Tier 1a `signal_params.enabled` (inner, DB-backed) | `scout/trading/engine.py:open_trade` step 0b | When False, `engine.open_trade` returns None with `trade_skipped_signal_disabled` log |
| (Future) per-channel/per-curator | not yet built | Tier 2b |

Three layers compose without interference — outer wins by short-circuiting before the DB check.

### Chain pattern lifecycle

`scout/chains/patterns.py:run_pattern_lifecycle` retires patterns when `total_evaluated >= CHAIN_MIN_TRIGGERS_FOR_STATS=10` AND `hit_rate < _RETIREMENT_HIT_RATE=0.20`. **BL-071 systemic-zero-hits guard** at lines 308-315: if ALL patterns show 0 hits across the trigger floor, short-circuit before retirement (the cause is upstream telemetry failure, not pattern quality). Guard verified by 2 unit tests in `tests/test_chains_learn.py`.

### Test pattern

pytest-asyncio auto mode (`asyncio_mode = "auto"` in `pyproject.toml`). `tmp_path` for DB fixtures. `tests/conftest.py` ships `settings_factory(**overrides)` and `token_factory(**overrides)`. HTTP mocks via `aioresponses`. Every public function gets a corresponding test; existing scaffold tests must never regress.

### Plan/design/spec convention (mechanical)

Every `tasks/(plan|design|spec)_*.md` file MUST start with:

> `**New primitives introduced:** [list, or NONE]`

The hook at `.claude/hooks/check-new-primitives.py` blocks Write/Edit/MultiEdit/NotebookEdit on gated files lacking this line. Markers inside ```` ``` ```` code fences do NOT count. Tolerant regex: case-insensitive, optional bold, optional whitespace, colon mandatory.

Bypass via `<!-- new-primitives-check: bypass -->` (logged to `.claude/hooks/bypass.log` for PR-time review).

---

## Part 2 — Operational drift checklist

**Organizing principle: silent-failure first, loud-failure later.**

A bug that breaks loudly is a 4am page; you fix it and move on. A bug that breaks silently — like chain_patterns auto-retiring for 17 days because no convention surfaced the failure — hurts for days before anyone knows to look.

**Format rule:** every entry has owner + due-date OR explicit `deferred-indefinitely (reason)`. Bare TODOs are not allowed; they become a graveyard.

| Item | Failure mode | State | Owner / Target |
|---|---|---|---|
| chain_patterns auto-retire on stale outcome telemetry | Patterns silently disabled when ALL show 0% hit rate; chain_matches stops being written; trading layer blind | Guard live (BL-071, PR #61) | done 2026-05-03 |
| memecoin `outcomes` table is empty (BL-071a) | Memecoin chain_matches can never be hydrated; permanently stuck at `outcome_class=NULL` or `EXPIRED` | Known root cause; investigation deferred | deferred-research; revisit when data volume warrants OR if BL-071 guard is loosened |
| narrative chain_matches start at `outcome_class='EXPIRED'` (BL-071b) | Hydrator's `WHERE outcome_class IS NULL` skips them; 42 actual HIT predictions in `predictions` table never propagate to chain_matches | Known root cause; investigation deferred | deferred-research; revisit when data volume warrants OR if BL-071 guard is loosened |
| `TELEGRAM_BOT_TOKEN` placeholder in prod `.env` | Every alert path silently 404s — calibrate.py, auto_suspend, channel-silence, BL-063, BL-064 dispatches | Known | deferred-explicitly per operator instruction (multiple times) |
| narrative_prediction token_id divergence | 32 of 56 stale-young open trades have empty/synthetic token_ids missing from `price_cache` | Known | deferred-pending-evidence; revisit when more open trades accumulate |
| BL-064 listener requires pipeline restart for new channels | New curator additions need full restart to be monitored | Known operational gap | deferred-pending-priority; not blocking current 6-channel curator set |

### Discipline for items added later

Each new item gets: description, **silent-vs-loud failure mode**, realistic time estimate (not "5-minute fix" unless it really is), current state, target date or explicit `deferred-indefinitely (here's why)`.

The defer-with-reason cases are honest in a way that pure "pending" isn't. If an item sits in "pending" for >3 months without a target, that's a signal to either commit to it or move it to `deferred-indefinitely`.

---

## Part 3 — Working agreement

The structural fix for "drift via imported priors" is reading deployed code before proposing.

### The rule

**Before proposing schema, test, or architecture work, read the relevant deployed code first.**

| Work type | Read first (mandatory before drafting) |
|---|---|
| Schema / migration | `scout/db.py` (`_create_tables`, recent `_migrate_*` methods) |
| Tier 1a params | `scout/trading/params.py` + recent `signal_params_audit` rows |
| New scoring signal | `scout/scorer.py` (~180 lines, all signals defined inline) |
| New paper-trade signal_type | `scout/trading/signals.py` dispatchers + `scout/trading/engine.py:open_trade` |
| New evaluator exit logic | `scout/trading/evaluator.py:evaluate_paper_trades` (~430 lines, ladder + moonshot + peak-fade interleave is load-bearing) |
| Test work | `tests/conftest.py` + 1-2 existing tests in the same domain (e.g. `tests/test_signal_params*.py`) |
| Dashboard endpoint | `dashboard/api.py:create_app` (existing endpoints show the pattern) |
| Chain pattern logic | `scout/chains/patterns.py` + `scout/chains/tracker.py` |
| Audit-log entries | search `signal_params_audit`, `paper_migrations` for prior writers |
| File-locking / atomic writes | n/a — gecko-alpha uses sqlite WAL, not flat-file state |
| Hook implementation | `.claude/settings.json` for existing hook conventions; `.claude/hooks/` for prior scripts |

This rule eliminates ~80% of corrections at zero new infrastructure cost. Most "drift" in this project's history has been a contributor (human or AI) importing a SaaS-style frame before grounding in this codebase's specific shape.

### New-primitives-declaration convention (mechanical)

Every `tasks/plan_*.md` / `tasks/design_*.md` / `tasks/spec_*.md` MUST begin with:

> `**New primitives introduced:** [list, or NONE]`

This single line answers: "what new infrastructure does this proposal add?" Reviewers can skim the list to spot bloat ("does this really need 3 new tables and a service?") or missing surface ("you didn't mention that this requires a new column").

The hook at `.claude/hooks/check-new-primitives.py` enforces this mechanically. It:
- Blocks `Write|Edit|MultiEdit|NotebookEdit` on gated files lacking the marker
- Strips ```` ``` ``` ```` code fences before matching (so an alignment doc that documents the marker as an example doesn't satisfy itself)
- Tolerates typos: case-insensitive, optional bold, optional whitespace
- Allows bypass via `<!-- new-primitives-check: bypass -->` (logged to `.claude/hooks/bypass.log`)

### Asymmetric workflow

The rule is the same in both directions, but operationalization differs:

- **Contributor with repo access (Claude Code, human dev):** read directly. ~1 second per `Read`/grep call. No excuse to skip.
- **External reviewer or remote agent:** ask the user to share relevant deployed code before drafting. ~30s round-trip; user can pre-emptively share files when starting an architectural thread to skip the round-trip.

In both cases the principle is "frame from deployed code, not from priors."

---

## Part 4 — What this doc is NOT

- **Not gecko-alpha philosophy.** gecko-alpha is a paper-trading pipeline; it doesn't have a philosophy. This doc captures patterns *we* deployed. Don't quote bullets here as "gecko-alpha prefers X" in future arguments.
- **Not prescriptive about future patterns.** When a new requirement doesn't fit cleanly into Part 1, the answer is to update Part 1 with the new pattern, not to refuse the requirement because it doesn't match.
- **Not a substitute for reading code.** Part 1 summarizes; deployed code is authoritative. If they conflict, deployed code wins and Part 1 is stale.
- **Not enforcement.** The doc is reference. The `.claude/hooks/check-new-primitives.py` hook is the enforcement. They serve different roles. If the hook is disabled, the doc has no teeth — and that's a known limitation, not a bug.
- **Not a hook truthiness check.** The hook checks the marker EXISTS. It does NOT validate that the listed primitives are TRUTHFUL or COMPLETE. A plan that writes `**New primitives introduced:** NONE` while introducing a new table satisfies the hook but is wrong. Validating accuracy of the list is the responsibility of human PR review.
- **Not a replacement for code review.** Individual PRs still need review against actual changes. This doc raises the floor; review keeps the ceiling.
- **Not a dictate from outside.** The patterns here emerged from how this project actually evolved. Treat the doc as documentation, not policy.
- **Not done.** v1 captures 2026-05-03 state. Update as state changes.

### Cross-platform note

Hook command uses `uv run python .claude/hooks/check-new-primitives.py`. On Windows + Git Bash this works because `uv` is installed at a discoverable PATH location (per project convention). If `uv` is not on PATH, the hook will fail-closed (block all writes), which is the safer failure mode but operationally annoying — fix by ensuring `uv` is on PATH.
