# BL-064 channel-reload v2: TypedDict channels_holder — combined plan + design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**New primitives introduced:** new `TypedDict` `ChannelsHolder` in `scout/social/telegram/listener.py` (replaces the bare `dict` annotation on the mutable container shared between `_run_listener_body` + `_make_channel_reload_heartbeat`); type-safer call site without runtime behavior change. NO new DB tables, columns, settings, migrations, or dependencies.

**Combined plan + design rationale:** scope is 1 type alias + 4 annotation sites. Full plan + 2 reviewers + design + 2 reviewers cycle is ceremony; combined doc + 2 reviewers preserves rigor.

---

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Python TypedDict introduction | None (universal language feature) | Build inline |
| Mutable-container typing pattern | None | Build inline |

**Verdict:** Pure type-annotation refinement. No external skill applicable.

---

## Drift grounding

**Read before drafting (verified — line numbers refreshed against branch sha 532df64):**
- `scout/social/telegram/listener.py:15` — existing `from typing import Any, Literal` (extend with `TypedDict`).
- `scout/social/telegram/listener.py:1193` — `channels_holder = {"channels": channels}` instantiation in `_run_listener_body`.
- `scout/social/telegram/listener.py:1195` — passed positionally to `_make_channel_reload_heartbeat`.
- `scout/social/telegram/listener.py:1309` — `channels_holder: dict` parameter (the bare-dict annotation triggering the NIT).
- `scout/social/telegram/listener.py:1355,1358` — heartbeat closure write/read; pyright infers from captured TypedDict, no annotation needed.
- `tests/test_bl064_channel_reload.py:200,231` — test mocks construct bare `{"channels": [...]}` literal. **TypedDict is structural in CPython** — bare dict satisfies type at runtime; tests pass unchanged.

**v2 plan-review fixes (item1-adv `ab2139f6` + item1-arch `a900a032`):**
- adv-SF1: line numbers refreshed (was 1190/1245/1308; stale ~47 lines vs actual 1193/1195/1309).
- adv-N1 / arch-D3: extend existing `from typing import Any, Literal` line (NOT a separate import).
- arch-S1: `@dataclass` rebuttal strengthened — also requires rewriting the dict-literal at line 1193 + every subscript access at 1355/1358 → ~6 LOC invasive vs 0 LOC for TypedDict.
- arch-D4: TypedDict growth path acknowledged: future fields can be `NotRequired[...]` so existing `{"channels": ...}` literals stay valid.
- arch-D5: keep module-private (no `__all__` export); only 2 callers in same module.
- PR #73 architecture review (a1c3edcb) §1+§6: "`channels_holder: dict` is the only untyped seam in an otherwise type-hinted module — gives pyright something to lint." Recommendation: TypedDict OR `@dataclass(slots=True)`. NIT, not blocker.

**Pattern conformance:**
- TypedDict matches `from typing import TypedDict` Python 3.12 stdlib pattern; no new dependency.
- Module already uses type hints throughout (`db: Database`, `settings: Settings`, etc.).

---

**Goal:** Replace `dict` with `TypedDict` annotation so static type-checkers can verify `channels_holder["channels"]` accesses are well-typed (`list[str]`).

**Architecture:** Add a single `class ChannelsHolder(TypedDict): channels: list[str]` near other type definitions in `listener.py`. Update 3 sites:
1. The `channels_holder = {"channels": channels}` instantiation (add inline type comment OR explicit cast).
2. The `_make_channel_reload_heartbeat(... channels_holder: dict, ...)` parameter.
3. Same on `_channel_reload_once` if applicable (it doesn't actually reference `channels_holder` — it takes `in_memory: list[str]` directly, so no change there).

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `scout/social/telegram/listener.py` | Add `ChannelsHolder` TypedDict + update 2 type annotations | Modify |
| `tests/test_bl064_channel_reload.py` | (no changes needed — TypedDict is structural; existing tests still pass) | No change |

---

## Tasks

### Task 1: Define TypedDict + update annotations

- [ ] **Step 1: Add TypedDict definition near top of listener.py imports**

```python
from typing import TypedDict

class ChannelsHolder(TypedDict):
    """BL-064 channel-reload mutable container shared between
    `_run_listener_body` and `_make_channel_reload_heartbeat`. The
    factory + heartbeat read/write `channels` to coordinate the in-memory
    channel list across the closure boundary without `nonlocal`.

    PR #73 architecture-review NIT cleanup: was bare `dict` annotation;
    upgraded to TypedDict so pyright can verify `holder["channels"]`
    yields `list[str]`.
    """

    channels: list[str]
```

- [ ] **Step 2: Update the heartbeat factory signature**

In `scout/social/telegram/listener.py:_make_channel_reload_heartbeat`:

```python
def _make_channel_reload_heartbeat(
    db: Database,
    client,
    settings,
    channels_holder: ChannelsHolder,  # was: dict
    on_new_handler,
):
```

- [ ] **Step 3: Type-annotate the instantiation in `_run_listener_body`**

```python
    channels_holder: ChannelsHolder = {"channels": channels}
```

- [ ] **Step 4: Run regression sweep**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl064_channel_reload.py tests/test_config.py -q
```

Expected: 9 tests still passing (6 listener-gated + 3 validator). No regressions.

- [ ] **Step 5: Commit**

```bash
git add scout/social/telegram/listener.py
git commit -m "refactor(BL-064 reload): TypedDict channels_holder per PR #73 NIT

PR #73 architecture review noted channels_holder: dict was the only
untyped seam in the module. TypedDict gives pyright something to lint
without runtime behavior change. Existing tests unchanged + still pass.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Test matrix

| ID | Test | Status |
|---|---|---|
| All existing | `tests/test_bl064_channel_reload.py` (6 tests) | Pre-existing — must not regress |
| All existing | `tests/test_config.py` (3 BL-064 validator tests) | Pre-existing — must not regress |

No new tests — TypedDict is a static type annotation; runtime behavior is identical to `dict`.

---

## Failure modes (silent-failure-first)

| # | Failure | Silent or loud? | Mitigation | Residual risk |
|---|---|---|---|---|
| F1 | `TypedDict` typo (e.g., `channesl: list[str]`) — runtime still works because TypedDict is structural | **Silent** at runtime; **Loud** under mypy/pyright | Project doesn't enforce mypy in CI today; loudness depends on operator workflow | Acceptable — change is intent-documenting, not behavior-enforcing |
| F2 | Future caller of `_make_channel_reload_heartbeat` passes a `dict` literal that doesn't match `ChannelsHolder` shape | **Silent** at runtime (TypedDict doesn't enforce shape); **Loud** under static check | Acceptable — same as F1; relies on type-checker being run | None at runtime |
| F3 | Python version <3.8 lacks `TypedDict` in `typing` (would need `typing_extensions`) | **Loud** (ImportError at module load) | Project pins Python 3.12 (per CLAUDE.md / pyproject.toml); `typing.TypedDict` available | None |

---

## Performance notes

Zero runtime impact — TypedDict is purely static. No bytecode change beyond the class definition itself.

---

## Rollback

Pure code revert. No DB / Settings / migration. `git revert <sha>` returns to the bare `dict` annotation.

---

## Operational verification (§5)

No deploy verification needed beyond standard "service active+running" — TypedDict has no runtime side effects.

Stop-FIRST sequence (BL-076 lesson):
1. `systemctl stop gecko-pipeline`
2. `git pull origin master`
3. `find . -name __pycache__ -type d -exec rm -rf {} +`
4. `systemctl start gecko-pipeline`
5. `systemctl is-active gecko-pipeline` → expect `active`

---

## Self-Review

1. **Hermes-first:** ✓ 2/2 negative. Pure project-internal type refinement.
2. **Drift grounding:** ✓ explicit file:line refs to PR #73 architecture review notes.
3. **Test matrix:** existing tests preserved; no new tests (TypedDict is static).
4. **Failure modes:** 3 enumerated; all silent-at-runtime, loud-under-static-check; acceptable for an intent-documenting change.
5. **Performance:** zero impact.
6. **Rollback:** pure code revert.
7. **Combined plan+design:** scope is ~10 LOC; full pipeline overkill.
8. **Honest scope:**
   - **NOT in scope:** runtime validation of channels_holder shape.
   - **NOT in scope:** mypy/pyright integration in CI (operator-side decision).
   - **NOT in scope:** `@dataclass` alternative (TypedDict matches the JSON-shape access pattern; dataclass would require attribute-access changes throughout).
