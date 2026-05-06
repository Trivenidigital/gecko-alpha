**New primitives introduced:** Per-laggard `category_heating` event emission semantics (move emission INTO the laggards-scoring loop in `scout/narrative/agent.py`, change `token_id` from `accel.category_id` to `token.coin_id`). NO new schema, NO new config, NO new code modules. The `event_data` payload retains `category_id` + `name` so dashboard / pattern-condition consumers keep parity.

# Plan: narrative_prediction chain-coherence — per-laggard `category_heating` emission (BL-NEW-CHAIN-COHERENCE)

## Hermes-first analysis

**Domains checked against the Hermes skill hub at `hermes-agent.nousresearch.com/docs/skills`:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Internal pipeline event-emission re-keying | none found — pure scout-side event-store semantics | build from scratch (single-emission-site fix) |
| Chain pattern matching across token_id | covered by existing `scout/chains/tracker.py` + `scout/chains/patterns.py` | use existing infrastructure (no changes there) |

**Awesome-hermes-agent ecosystem check:** none relevant — agent-orchestration index, not signal-store-keying.

**Drift-check (per global CLAUDE.md §7a):**
- `grep -rn "category_heating" scout/ tests/ dashboard/` → confirmed two emission sites (one production at `agent.py:174`, plus pattern-definition references in `chains/patterns.py:66,100`)
- `grep -rn "32 of 56\|stale-young" scout/ tests/` → 0 hits in code (only in docs)
- Original symptom (paper_trades token_id divergence) verified resolved on 2026-05-06: prod scout.db has 0 empty + 0 unresolved token_ids across 67 open + 100 closed narrative_prediction trades. PR #72 gate + zombie cleanup already addressed it.

**Verdict:** the original todo item ("upstream fix") is still real BUT the symptom moved. The remaining structural issue is signal_events pollution — 2,770 `category_heating` rows keyed by category_id break chain pattern matching for `full_conviction` + `narrative_momentum`. Building from scratch (single emission-site fix) is the only path.

---

## Goal

Restore chain-pattern coherence. The chain patterns in `scout/chains/patterns.py:56-160` define `full_conviction` and `narrative_momentum` to anchor on `category_heating` and chain through `laggard_picked` → `narrative_scored` → `counter_scored` for the **same token**. The chain tracker matches sequences by `token_id`. Currently the anchor's `token_id` is a category id (e.g. `ton-meme-coins`) and subsequent steps use real coin ids (e.g. `pepe`) — they can never share a token_id, so the chain patterns never match.

Production evidence (2026-05-06 02:30Z):
- `category_heating` events: 2,770 (159 distinct category-id token_ids)
- `laggard_picked` events: 546 (286 distinct coin_id token_ids)
- `chain_complete` events: 2 (in entire history, not 2,770 attempts × 286 candidates)

The expected fix moves the `category_heating` emission INTO the laggards-scoring loop and emits ONE event per laggard with `token_id=token.coin_id`. The original category metadata (id, name, acceleration, volume_growth_pct, market_regime) is preserved in `event_data`, so:
- Dashboard query at `dashboard/db.py:744-770` is unaffected (it reads `narrative_signals` table, not `signal_events`).
- Pattern conditions reading `event_data["category_id"]` still work.
- Chain tracker can now match anchor + downstream events on the same token_id.

## Scope explicitly OUT

- Existing 2,770 polluted rows are LEFT alone — they prune via existing `prune_old_events` mechanism (default retention applies). They never matched a chain anyway.
- No schema change to `signal_events`.
- No new config knob.
- No change to `is_cooling_down` / `record_signal` / `narrative_signals` table behavior — those are category-keyed by design and correctly so.
- No change to dashboard queries.

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/narrative/agent.py` | Modify | Move `category_heating` emission INTO the laggards loop (line 257+), change token_id to `token.coin_id`. The pre-loop block at line 172-187 currently emits ONCE per category before laggards are even fetched — that's the bug location. |
| `tests/test_narrative_chain_coherence.py` | Create | New test pins the per-laggard semantics: assert every `category_heating` event in `signal_events` has a `token_id` that EXISTS as `token_id` on at least one paired `laggard_picked` event in the same agent run. |

No other production code touched. Test files for chains/* unchanged — the tracker / pattern-matching code already correctly matches by token_id; the bug was only at the emission site.

## Tasks

### Task 1: Failing TDD test for per-laggard emission

`tests/test_narrative_chain_coherence.py` (NEW):

```python
"""BL-NEW-CHAIN-COHERENCE: per-laggard category_heating emission tests."""
from __future__ import annotations

# Test pins:
#   1. category_heating now fires ONCE PER LAGGARD (not once per category)
#   2. Each category_heating event's token_id MATCHES the laggard's coin_id
#   3. category_heating + laggard_picked + narrative_scored share the same
#      token_id within one agent invocation, enabling chain pattern matching
```

Assertions:
- After mocking heating + 3 laggards in 1 category, `signal_events` table has ≥3 `category_heating` rows (one per laggard).
- For each `category_heating` row, there exists a paired `laggard_picked` row with the SAME `token_id`.
- Zero `category_heating` rows have `token_id` matching the synthetic `accel.category_id`.

### Task 2: Move emission

In `scout/narrative/agent.py`, delete the line-174 emission block. Re-emit inside the laggards-scoring loop just before the existing `laggard_picked` emission at line 298 (so chain ordering is preserved: heating-anchor first, then picked, then scored). New emission:

```python
# Per-laggard category_heating emission (BL-NEW-CHAIN-COHERENCE 2026-05-06).
# Anchors the chain pattern on the laggard's coin_id so subsequent
# laggard_picked / narrative_scored / counter_scored events on the same
# token can match. Previously emitted once-per-category with
# token_id=category_id which broke chain matching since downstream
# events use coin_id.
await safe_emit(
    db,
    token_id=token.coin_id,
    pipeline="narrative",
    event_type="category_heating",
    event_data={
        "category_id": accel.category_id,
        "name": accel.name,
        "acceleration": accel.acceleration,
        "volume_growth_pct": accel.volume_growth_pct,
        "market_regime": market_regime,
    },
    source_module="narrative.observer",
)
```

### Task 3: Verify existing chain tests still pass

`tests/test_chains_tracker.py` and friends use synthetic event sequences with same-token-id chains. They were already correct (testing the matcher, not the emission). Run them — they should remain green.

### Task 4: Run full chain regression

```bash
uv run pytest tests/test_chains_*.py tests/test_narrative_chain_coherence.py tests/test_narrative_*.py -q
```

All green expected (no behavior change in matcher, only at emission site).

### Task 5: Black + commit

### Task 6: PR + 3 reviewers + fix + merge + deploy

3-vector reviewer dispatch (statistical / code / strategy).

---

## Done criteria

- New tests pass
- Existing chain tests unchanged (smoke check)
- PR merged, deployed
- Operator-visible improvement: `chain_complete` events should start firing more frequently going forward (not validated this session — observation over 7d).
- todo.md updated to close #4 (the original "upstream fix" item).
