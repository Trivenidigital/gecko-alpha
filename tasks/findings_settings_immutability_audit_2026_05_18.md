**New primitives introduced:** NONE.

# BL-NEW-SETTINGS-IMMUTABILITY — Audit-Phase Findings 2026-05-18

**Data freshness:** Computed against worktree HEAD = `cdeb31f` = origin/master (includes PRs #150-#154 from cycle 12).

**Source:** Cross-tree grep for Settings mutation patterns (`^\s*settings\.[A-Z_0-9]+\s*=`, `setattr(settings`, `object.__setattr__(settings`, `Settings(**`, `monkeypatch.setattr.*[Ss]ettings`).

**Drift-check:** worktree HEAD = origin/master (zero divergence). No parallel session on `BL-NEW-SETTINGS-IMMUTABILITY`.

**Hermes-first verdict:** In-tree Pydantic Settings model — no Hermes primitive applies; pure Python/Pydantic design decision.

## TL;DR

**Recommendation: SHIP FINDINGS DOC ONLY. Do NOT implement `frozen=True` at this time.**

The originating concern (V6 PR-review NOTE from `feat/score-volume-pruning-harden`) was that post-construction mutation could silently bypass `_validate_retention_covers_secondwave_window`. Audit confirms: **no current code path mutates `SCORE_HISTORY_RETENTION_DAYS`** (the validator's target field). The single production mutation site is `scout/main.py:1534` (CLI override of `MIN_SCORE`), which has no validator-relevant interaction.

Implementing `frozen=True` would require refactoring 1 production + ~10 test mutation sites for protection against a purely hypothetical future bug. Cost > benefit at this time. File as deferred follow-up with explicit convention guidance.

## Mutation site classification

### Category 1: Legitimate production runtime override (1 site)

| Site | Code | Classification | Risk to validators |
|---|---|---|---|
| `scout/main.py:1534` | `settings.MIN_SCORE = args.min_score_override` | Legitimate CLI override. Pre-populated via `configure_cache(settings)` at L1532; mutation happens before the override-aware logger emits. Operator-initiated via explicit `--min-score-override` flag. | NONE — no `MIN_SCORE` validator exists; the field is plain `int = 60` per `scout/config.py:27`. |

### Category 2: Test-only direct mutations (~10 sites)

| Site | Pattern | Classification |
|---|---|---|
| `tests/test_main.py:14-17` | 4× `settings.SCAN_INTERVAL_SECONDS = / .MIN_SCORE = / .DB_PATH = / .PERP_ENABLED =` | Test-only setup; isolates main loop behavior |
| `tests/test_trading_engine.py:147,148,193,194,213,214` | 6× `settings.PAPER_*` mutations | Test-only setup for paper-trade isolation |

Both files use direct `settings.X = value` rather than `monkeypatch.setattr(settings, "X", value)`. Direct mutation is broader-effect than monkeypatch (changes persist across tests if `settings` is a session-scope fixture). If the test fixture is function-scoped, behavior is acceptable; if session-scoped, this is latent test pollution.

### Category 3: monkeypatch.setattr (pytest-managed, 1 site + plan-doc examples)

| Site | Pattern | Classification |
|---|---|---|
| `tests/test_bl076_junk_filter_and_symbol_name.py:130` | `monkeypatch.setattr(settings, "PAPER_STARTUP_WARMUP_SECONDS", 10)` | Clean — pytest auto-reverts after test. **Preferred pattern.** |
| `tasks/plan_bl067_conviction_lock.md` (×6) | `monkeypatch.setattr(settings, ...)` examples in plan doc | Documentation only. |

### Category 4: `Settings(**defaults)` construction (~25 sites)

All construction-time. NOT post-construction mutation. **Not in scope** for the immutability concern — Pydantic validators fire at construction. This is the canonical safe pattern.

### Category 5: No unsafe mutations found

- 0 production mutations of `SCORE_HISTORY_RETENTION_DAYS` (the originating validator's target)
- 0 production mutations of `LIVE_DAILY_LOSS_CAP_USD` / `LIVE_MAX_EXPOSURE_USD` (other live-caps validator targets)
- 0 production `setattr()` / `object.__setattr__()` on settings
- 0 production `Settings(**)` calls outside `scout/config.py:1319` (the load helper)

## Why `frozen=True` is NOT recommended now

| Cost | Benefit |
|---|---|
| Refactor `scout/main.py:1534` to use `settings.model_copy(update={"MIN_SCORE": ...})` + plumb new settings instance through the cache (~30min + test impact) | Protect against hypothetical future mutation that bypasses a validator |
| Refactor 10+ test mutation sites in `tests/test_main.py` + `tests/test_trading_engine.py` to use `monkeypatch.setattr` (~2-3h + test debugging if any reveal coupling) | (same — hypothetical) |
| Risk of breaking tests during conversion (Pydantic-frozen behavior may surface latent test coupling) | (same — hypothetical) |
| `model_copy(update=...)` produces a NEW instance; callers retaining the OLD reference see the unchanged value — broader pattern change | (same — hypothetical) |

The originating V6 concern (`SCORE_HISTORY_RETENTION_DAYS` bypass) does NOT have a current mutation path. Implementing `frozen=True` would be **defense against a bug that doesn't exist** at the cost of a real test refactor.

## What to do instead (cheap, doc-only)

1. **Convention documentation** — Add to `scout/config.py` module docstring: "Do NOT mutate `settings.<KEY>` post-construction in production code. Validators fire at construction time only. Use `settings.model_copy(update={...})` for legitimate overrides. Test-only mutations should use `monkeypatch.setattr(settings, 'KEY', value)`."

2. **Audit-aware test review** — Future PRs that add `settings.X = value` in production code (outside the `scout/main.py:1534` precedent) should be flagged in code review. Document the policy.

3. **Filed follow-up** — `BL-NEW-SETTINGS-FROZEN-WHEN-CALL-FOR-IT` deferred-evidence-gated: re-evaluate `frozen=True` when (a) any validator is added with a high-blast-radius invariant (e.g., live-caps, soak-window, etc.) AND (b) any production mutation of that validator's target field is proposed.

## Re-evaluation triggers

Re-run this audit when:
1. A new Pydantic validator is added with a load-bearing invariant (e.g., money flows, soak windows, schema migration prerequisites)
2. Any production code adds a `settings.X = value` post-construction mutation outside `scout/main.py:1534`
3. Calendar: 2026-08-18 (90d backstop)

## Cross-references

- `backlog.md` BL-NEW-SETTINGS-IMMUTABILITY (originating L1526; flipping to AUDITED 2026-05-18)
- V6 PR-review NOTE from `feat/score-volume-pruning-harden` (originating finding)
- `scout/config.py:27` (`MIN_SCORE = 60`) — the only mutated production setting
- `scout/main.py:1534` (the only production mutation site)
- `scout/config.py:1319` (`Settings(**kwargs)` — load helper; construction-time, not mutation)
- Memory: `feedback_in_memory_telemetry_persistence.md` (related pattern: module-level state needs careful handling)
- CLAUDE.md §9a (runtime-state verification — applies here as "verify the mutation surface before adding frozen=True")
