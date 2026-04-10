# Multi-Signal Conviction Chains — Design Spec

**Date:** 2026-04-10
**Status:** Draft
**Goal:** Correlate signals from different modules into temporal chains that match known pump patterns. Instead of scoring each signal independently, detect when signals fire in a specific SEQUENCE that historically predicts sustained pumps.
**Module:** `scout/chains/` — independent module consuming events from existing pipeline
**Cost:** Zero additional API calls (operates on data already produced by other modules)
**Deployment:** Runs inside gecko-alpha as a lightweight async loop alongside existing pipeline

---

## 1. Architecture Overview

```
┌────────────────────────────────────────────────────────────────┐
│                 EVENT EMITTERS (existing modules)               │
│  narrative/observer.py  → "category_heating"                    │
│  narrative/predictor.py → "laggard_picked", "narrative_scored"  │
│  scorer.py              → "candidate_scored"                    │
│  gate.py                → "conviction_gated"                    │
│  alerter.py             → "alert_fired"                         │
│  counter/scorer.py      → "counter_scored"                      │
│  evaluator.py           → "second_wave_detected" (future)       │
└──────────────────────┬─────────────────────────────────────────┘
                       ▼
┌────────────────────────────────────────────────────────────────┐
│              EVENT STORE (signal_events table)                   │
│  token_id + event_type + event_data (JSON) + timestamp          │
│  Append-only. Pruned after 14 days.                             │
└──────────────────────┬─────────────────────────────────────────┘
                       ▼
┌────────────────────────────────────────────────────────────────┐
│              CHAIN TRACKER (runs every 5 min)                   │
│  For each token with recent events:                             │
│    Check all active chain_patterns                              │
│    If pattern steps match within time windows → advance chain   │
│    If chain completes (min_steps_to_trigger met):               │
│      → Emit "chain_complete" event                              │
│      → Boost conviction score                                   │
│      → Fire high-confidence alert                               │
└──────────────────────┬─────────────────────────────────────────┘
                       ▼
┌────────────────────────────────────────────────────────────────┐
│              LEARN INTEGRATION (daily)                           │
│  Compare chain-complete tokens vs non-chain tokens              │
│  Compute per-pattern hit rate from outcomes table               │
│  Retire patterns with hit_rate < baseline                       │
│  Adjust time windows based on observed timing distributions     │
└────────────────────────────────────────────────────────────────┘
```

---

## 2. Module Structure

All new code lives in `scout/chains/`. No existing files are modified except `scout/db.py` (new tables), `scout/main.py` (add tracker loop to gather), `scout/config.py` (new config keys), and minimal 1-line event emission calls in existing modules.

```
scout/chains/
  __init__.py
  events.py        # Event emitter + signal_events table CRUD
  patterns.py      # Chain pattern definitions + step matching logic
  tracker.py       # Main chain tracking loop
  models.py        # ChainEvent, ChainPattern, ChainStep, ChainMatch, ActiveChain
  alerts.py        # High-conviction chain alert formatting
```

---

## 3. Pydantic Models (`scout/chains/models.py`)

```python
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel


class ChainEvent(BaseModel):
    """A single signal event emitted by any module."""
    id: int | None = None
    token_id: str                      # contract_address or coin_id
    event_type: str                    # e.g. "category_heating", "counter_scored"
    event_data: dict                   # signal-specific payload (JSON)
    source_module: str                 # e.g. "narrative.observer", "scorer"
    created_at: datetime


class ChainStep(BaseModel):
    """One step in a chain pattern definition."""
    step_number: int                   # 1-based ordering
    event_type: str                    # must match ChainEvent.event_type
    condition: str | None = None       # optional JSONPath-like condition on event_data
                                       # e.g. "risk_score < 30", "narrative_fit_score > 60"
    max_hours_after_anchor: float      # max hours after step 1 (anchor) for this step
    max_hours_after_previous: float | None = None  # optional: max hours after prior step


class ChainPattern(BaseModel):
    """A configurable chain pattern definition."""
    id: int | None = None
    name: str                          # e.g. "full_conviction", "narrative_to_volume"
    description: str
    steps: list[ChainStep]
    min_steps_to_trigger: int          # how many steps must match to fire
    conviction_boost: int              # points to add to conviction score (0-30)
    alert_priority: str                # "high" | "medium" | "low"
    is_active: bool = True
    historical_hit_rate: float | None = None   # computed by LEARN phase
    total_triggers: int = 0
    total_hits: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActiveChain(BaseModel):
    """Tracks an in-progress chain for a specific token."""
    id: int | None = None
    token_id: str
    pattern_id: int
    pattern_name: str
    steps_matched: list[int]           # step_numbers that have matched
    step_events: dict[int, int]        # step_number -> signal_event_id
    anchor_time: datetime              # timestamp of step 1 match
    last_step_time: datetime           # timestamp of most recent matched step
    is_complete: bool = False
    completed_at: datetime | None = None
    conviction_boost_applied: bool = False
    created_at: datetime


class ChainMatch(BaseModel):
    """A completed chain — stored for LEARN phase analysis."""
    id: int | None = None
    token_id: str
    pattern_id: int
    pattern_name: str
    steps_matched: int                 # count of steps that fired
    total_steps: int                   # total steps in pattern
    anchor_time: datetime
    completed_at: datetime
    chain_duration_hours: float        # completed_at - anchor_time
    conviction_boost: int
    outcome_class: str | None = None   # HIT | MISS | NEUTRAL (filled by LEARN)
    outcome_change_pct: float | None = None
    evaluated_at: datetime | None = None
```

---

## 4. Event Store (`scout/chains/events.py`)

### Event emission

Every module that produces a meaningful signal calls a single function:

```python
async def emit_event(
    db: Database,
    token_id: str,
    event_type: str,
    event_data: dict,
    source_module: str,
) -> int:
    """Append a signal event to the event store. Returns the event ID."""
```

This is a thin INSERT into `signal_events`. No business logic — just timestamped append.

### Event types

Each existing module emits events at natural decision points. The emitter is a single `await emit_event(...)` call added to the existing code path — no structural changes to any module.

| Source Module | Event Type | When Emitted | event_data Payload |
|--------------|------------|-------------|-------------------|
| `narrative/observer.py` | `category_heating` | Category acceleration detected as heating | `{category_id, name, acceleration, volume_growth_pct, market_regime}` |
| `narrative/predictor.py` | `laggard_picked` | Token selected as laggard in heating category | `{category_id, category_name, narrative_fit_score, confidence, trigger_count}` |
| `narrative/predictor.py` | `narrative_scored` | Claude narrative-fit scoring complete | `{narrative_fit_score, staying_power, confidence}` |
| `scorer.py` | `candidate_scored` | Token scored by quant scorer | `{quant_score, signals_fired, signal_count}` |
| `gate.py` | `conviction_gated` | Token passes conviction gate | `{conviction_score, quant_score, narrative_score}` |
| `alerter.py` | `alert_fired` | Telegram/Discord alert sent | `{conviction_score, alert_type}` |
| `counter/scorer.py` | `counter_scored` | Counter-narrative scoring complete | `{risk_score, flag_count, high_severity_count, data_completeness}` |
| `chains/tracker.py` | `chain_complete` | A conviction chain completes | `{pattern_name, steps_matched, conviction_boost, chain_duration_hours}` |

### Token ID resolution

Events use `token_id` which maps to:
- For memecoin pipeline tokens: `contract_address` (already the primary key in `candidates`)
- For narrative pipeline tokens: `coin_id` (CoinGecko coin ID, primary key in `predictions`)

Since both pipelines can reference the same token (a CoinGecko coin that also appears on DexScreener), the chain tracker must handle both ID formats. The `event_data` payload includes both IDs when available, and the tracker matches on either.

### Retention

Signal events are pruned after 14 days (configurable via `CHAIN_EVENT_RETENTION_DAYS`). At ~100 events/hour worst case, 14 days = ~33K rows — well within SQLite comfort.

---

## 5. Chain Pattern Definitions (`scout/chains/patterns.py`)

### Built-in patterns (seeded on first run)

#### Pattern 1: `full_conviction` — Narrative-to-Volume Full Chain

The strongest pattern: a narrative heats, a laggard is picked, counter-score is clean, and volume confirms independently.

```
Steps:
  1. category_heating           (anchor — any time)
  2. laggard_picked             within 6h of step 1
  3. counter_scored              within 8h of step 1
     condition: risk_score < 30
  4. candidate_scored            within 12h of step 1
     condition: signal_count >= 3

Min steps to trigger: 3 of 4
Conviction boost: +25
Alert priority: high
```

Rationale: When narrative momentum, clean safety profile, and independent quant signals all converge on the same token within a 12-hour window, the probability of a sustained move is significantly higher than any single signal.

#### Pattern 2: `narrative_momentum` — Heating Category + Clean Counter

A lighter pattern for when narrative signals and safety checks align but the volume signal hasn't fired yet.

```
Steps:
  1. category_heating           (anchor)
  2. laggard_picked             within 4h of step 1
  3. narrative_scored            within 4h of step 1
     condition: narrative_fit_score > 70
  4. counter_scored              within 6h of step 1
     condition: risk_score < 40

Min steps to trigger: 3 of 4
Conviction boost: +15
Alert priority: medium
```

Rationale: High narrative fit plus clean counter-score suggests the token genuinely belongs to a heating narrative and has no obvious red flags. Worth an early alert even before volume confirms.

#### Pattern 3: `volume_breakout` — Quant Signals Converging

Pure quantitative pattern: multiple independent quant signals fire in quick succession.

```
Steps:
  1. candidate_scored            (anchor)
     condition: signal_count >= 2
  2. candidate_scored            within 4h of step 1
     condition: signal_count >= 3 (score improved)
  3. counter_scored              within 6h of step 1
     condition: risk_score < 50
  4. conviction_gated            within 8h of step 1

Min steps to trigger: 3 of 4
Conviction boost: +20
Alert priority: medium
```

Rationale: When a token's quant score improves across successive scans (score velocity) and passes the conviction gate with a clean safety profile, it indicates genuine accumulation rather than a one-time spike.

### Pattern storage

Patterns are stored in the `chain_patterns` table (see Section 9) as JSON-serialized step definitions. This makes them configurable via the LEARN phase without code changes.

### Condition evaluation

Step conditions are simple comparisons evaluated against `event_data`:

```python
def evaluate_condition(condition: str, event_data: dict) -> bool:
    """Evaluate a simple condition against event data.
    
    Supported: "field < N", "field > N", "field >= N", "field <= N", "field == N"
    Returns True if condition is None (unconditional step).
    """
```

Only simple field comparisons are supported — no complex expressions, no nesting. This keeps the evaluator deterministic and easy to audit. If more complex conditions are needed in the future, extend with explicit named condition functions rather than a mini-language.

---

## 6. Chain Tracker (`scout/chains/tracker.py`)

### Main loop

The tracker runs every 5 minutes as an async task in the main pipeline's `asyncio.gather()`:

```python
async def run_chain_tracker(db: Database, settings: Settings) -> None:
    """Main chain tracking loop. Runs continuously."""
    while True:
        try:
            await check_chains(db, settings)
        except Exception:
            logger.exception("chain_tracker_error")
        await asyncio.sleep(settings.CHAIN_CHECK_INTERVAL_SEC)
```

### Check chains algorithm

Each cycle:

1. **Load active patterns** from `chain_patterns` where `is_active = True`
2. **Load recent events** from `signal_events` where `created_at > now - max_chain_window` (default: 24h)
3. **Group events by token_id**
4. **For each token with events:**
   a. Load any `active_chains` for this token
   b. For each active pattern not yet started for this token:
      - Check if any event matches step 1 (anchor). If so, create an `ActiveChain`.
   c. For each active chain (including newly created):
      - Check unmatched steps against new events
      - Validate time windows (hours since anchor, hours since previous step)
      - Evaluate step conditions against event_data
      - If a step matches, add to `steps_matched`
   d. **Check expiry:** If anchor_time + max_chain_window has passed and chain is incomplete, mark expired and delete
   e. **Check completion:** If `len(steps_matched) >= pattern.min_steps_to_trigger`, mark complete

5. **For each newly completed chain:**
   a. Store a `ChainMatch` record
   b. Apply conviction boost to the token's score (update `candidates` or `predictions` table)
   c. Emit a `chain_complete` event
   d. If `alert_priority` is "high" or "medium", format and send a high-conviction alert

### Deduplication

- A token can only have ONE active chain per pattern at a time
- Once a chain completes, a new chain for the same pattern+token cannot start for `CHAIN_COOLDOWN_HOURS` (default: 12, configurable)
- An event can participate in multiple different patterns simultaneously (e.g., a `counter_scored` event can advance both `full_conviction` and `narrative_momentum` chains)

### Performance

The tracker operates on in-memory data loaded from SQLite. With ~33K max events (14 days) and ~3-5 active patterns, the matching loop is O(events * patterns * steps) per cycle. At realistic volumes (~100 events/hour, 5 patterns, 4 steps each), this is trivial — sub-millisecond per cycle.

No external API calls. No Claude calls. Pure data matching.

---

## 7. High-Conviction Alerts (`scout/chains/alerts.py`)

### Alert format

When a chain completes, a special alert is sent via the existing `scout/alerter.py` infrastructure:

```
=== CONVICTION CHAIN COMPLETE ===
Pattern: {pattern_name} ({steps_matched}/{total_steps} steps)
Token: {token_name} ({ticker}) on {chain}

Timeline:
  T+0h:  {step_1_event_type} — {step_1_summary}
  T+2h:  {step_2_event_type} — {step_2_summary}
  T+4h:  {step_3_event_type} — {step_3_summary}

Chain duration: {duration}h
Historical hit rate: {hit_rate}% ({total_triggers} prior triggers)
Conviction boost: +{boost} points

Current scores:
  Quant: {quant_score}  Narrative: {narrative_score}
  Counter risk: {counter_risk_score}
  Conviction: {conviction_score} (boosted from {original_score})
```

### Alert routing

- `alert_priority: "high"` patterns: Telegram alert immediately
- `alert_priority: "medium"` patterns: Included in daily digest only (unless conviction_score > 80 after boost, then Telegram)
- `alert_priority: "low"` patterns: Logged only, no alert (for experimental patterns being validated by LEARN)

---

## 8. LEARN Phase Integration

### Outcome tracking

The LEARN phase (existing `narrative/evaluator.py` and outcome checker in `main.py`) already tracks token outcomes. The chain module piggybacks on this:

1. When `chain_matches` records are created, `outcome_class` and `outcome_change_pct` are NULL
2. A daily job queries `chain_matches` where `evaluated_at IS NULL` and `completed_at` is older than 48 hours
3. For each, look up the token's outcome from `outcomes` table (memecoin pipeline) or `predictions` table (narrative pipeline)
4. Update `chain_matches` with the outcome

### Per-pattern hit rate

Computed daily:

```python
async def compute_pattern_stats(db: Database) -> list[dict]:
    """For each pattern, compute hit rate from evaluated chain_matches."""
    # hit_rate = hits / (hits + misses)  — NEUTRAL excluded
    # Only patterns with >= 10 evaluated matches get a hit_rate
```

Results stored back on `chain_patterns.historical_hit_rate`, `total_triggers`, `total_hits`.

### Pattern lifecycle

1. **Incubation** (first 30 days): Pattern runs with `alert_priority: "low"` — no user-facing alerts. Events are tracked and outcomes measured.
2. **Promotion**: If hit_rate > baseline + 10% after 20+ triggers, promote to `alert_priority: "medium"`.
3. **Graduation**: If hit_rate > 50% after 50+ triggers, promote to `alert_priority: "high"`.
4. **Retirement**: If hit_rate < baseline after 30+ triggers, set `is_active = False`. Keep data for analysis.

The baseline is the overall pipeline hit rate (alerts without chain completion). This ensures chains add value beyond what the existing pipeline already achieves.

### Future: LEARN-generated patterns

Once enough signal_events data accumulates (60+ days), the LEARN phase could analyze event sequences that preceded HITs and propose new chain patterns. This is out of scope for v1 but the event store makes it possible.

---

## 9. Database Schema

### `signal_events`

```sql
CREATE TABLE IF NOT EXISTS signal_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id       TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    event_data     TEXT NOT NULL,          -- JSON
    source_module  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(token_id, event_type, created_at)  -- prevent duplicate emissions
);
CREATE INDEX IF NOT EXISTS idx_sig_events_token
    ON signal_events(token_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sig_events_type
    ON signal_events(event_type, created_at);
```

Retention: 14 days (configurable via `CHAIN_EVENT_RETENTION_DAYS`).

### `chain_patterns`

```sql
CREATE TABLE IF NOT EXISTS chain_patterns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    description         TEXT NOT NULL,
    steps_json          TEXT NOT NULL,      -- JSON array of ChainStep dicts
    min_steps_to_trigger INTEGER NOT NULL,
    conviction_boost    INTEGER NOT NULL DEFAULT 0,
    alert_priority      TEXT NOT NULL DEFAULT 'low',
    is_active           INTEGER NOT NULL DEFAULT 1,
    historical_hit_rate REAL,
    total_triggers      INTEGER DEFAULT 0,
    total_hits          INTEGER DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### `active_chains`

```sql
CREATE TABLE IF NOT EXISTS active_chains (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id       TEXT NOT NULL,
    pattern_id     INTEGER NOT NULL REFERENCES chain_patterns(id),
    pattern_name   TEXT NOT NULL,
    steps_matched  TEXT NOT NULL,           -- JSON array of step numbers
    step_events    TEXT NOT NULL,           -- JSON dict: step_number -> event_id
    anchor_time    TEXT NOT NULL,
    last_step_time TEXT NOT NULL,
    is_complete    INTEGER DEFAULT 0,
    completed_at   TEXT,
    conviction_boost_applied INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(token_id, pattern_id, anchor_time)
);
CREATE INDEX IF NOT EXISTS idx_active_chains_token
    ON active_chains(token_id, is_complete);
```

Cleanup: Expired and completed chains older than 7 days are pruned from `active_chains`. Completed chains are preserved in `chain_matches`.

### `chain_matches`

```sql
CREATE TABLE IF NOT EXISTS chain_matches (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id             TEXT NOT NULL,
    pattern_id           INTEGER NOT NULL REFERENCES chain_patterns(id),
    pattern_name         TEXT NOT NULL,
    steps_matched        INTEGER NOT NULL,
    total_steps          INTEGER NOT NULL,
    anchor_time          TEXT NOT NULL,
    completed_at         TEXT NOT NULL,
    chain_duration_hours REAL NOT NULL,
    conviction_boost     INTEGER NOT NULL,
    outcome_class        TEXT,              -- HIT | MISS | NEUTRAL | NULL
    outcome_change_pct   REAL,
    evaluated_at         TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chain_matches_pattern
    ON chain_matches(pattern_id, outcome_class);
CREATE INDEX IF NOT EXISTS idx_chain_matches_token
    ON chain_matches(token_id, completed_at);
```

---

## 10. Configuration (`scout/config.py` additions)

```python
# Chain tracking
CHAIN_CHECK_INTERVAL_SEC: int = 300          # 5 minutes
CHAIN_MAX_WINDOW_HOURS: float = 24.0         # max duration for any chain
CHAIN_COOLDOWN_HOURS: float = 12.0           # cooldown before same pattern+token can re-trigger
CHAIN_EVENT_RETENTION_DAYS: int = 14         # prune signal_events older than this
CHAIN_ACTIVE_RETENTION_DAYS: int = 7         # prune expired/completed active_chains
CHAIN_MIN_TRIGGERS_FOR_STATS: int = 10       # min triggers before computing hit rate
CHAIN_PROMOTION_THRESHOLD: float = 0.10      # hit_rate must exceed baseline by this much
CHAIN_GRADUATION_MIN_TRIGGERS: int = 50      # min triggers for "high" priority
CHAIN_GRADUATION_HIT_RATE: float = 0.50      # min hit rate for "high" priority
CHAIN_ALERT_ON_COMPLETE: bool = True         # send Telegram on high-priority chain completion
```

All values from Settings (Pydantic BaseSettings), overridable via `.env`.

---

## 11. Integration Points

### Minimal changes to existing modules

Each existing module gets ONE new line at its natural decision point:

```python
# In narrative/observer.py, after detecting heating:
await emit_event(db, category_id, "category_heating", {
    "category_id": cat.category_id, "name": cat.name,
    "acceleration": cat.acceleration, "volume_growth_pct": cat.volume_growth_pct,
    "market_regime": market_regime,
}, source_module="narrative.observer")

# In narrative/predictor.py, after selecting a laggard:
await emit_event(db, token.coin_id, "laggard_picked", {
    "category_id": accel.category_id, "category_name": accel.name,
    "narrative_fit_score": score_result["narrative_fit"],
    "confidence": score_result["confidence"],
    "trigger_count": trigger_count,
}, source_module="narrative.predictor")

# In scorer.py, after scoring:
await emit_event(db, token.contract_address, "candidate_scored", {
    "quant_score": score, "signals_fired": signals, "signal_count": len(signals),
}, source_module="scorer")

# In counter/scorer.py, after scoring:
await emit_event(db, token_id, "counter_scored", {
    "risk_score": result.risk_score, "flag_count": len(result.flags),
    "high_severity_count": sum(1 for f in result.flags if f.severity == "high"),
    "data_completeness": result.data_completeness,
}, source_module="counter.scorer")

# In gate.py, after gating:
await emit_event(db, token.contract_address, "conviction_gated", {
    "conviction_score": conviction, "quant_score": quant, "narrative_score": narrative,
}, source_module="gate")

# In alerter.py, after sending alert:
await emit_event(db, token.contract_address, "alert_fired", {
    "conviction_score": conviction, "alert_type": "telegram",
}, source_module="alerter")
```

### main.py integration

Add the chain tracker to the main async gather:

```python
# In run_pipeline():
await asyncio.gather(
    # ... existing tasks ...
    run_chain_tracker(db, settings),  # new
)
```

### Conviction score boost

When a chain completes for a candidate token, the tracker updates the token's conviction score:

```python
# In tracker.py, on chain completion:
boosted_score = min(100, current_conviction + pattern.conviction_boost)
await db.update_conviction_score(token_id, boosted_score)
```

This is a simple score addition, not a re-run of the full scoring pipeline. The boost is additive and capped at 100.

---

## 12. Testing Strategy

### Unit tests (`tests/test_chains_*.py`)

| Test | What it verifies |
|------|-----------------|
| `test_emit_event` | Event is stored in signal_events with correct fields |
| `test_emit_event_dedup` | Duplicate events (same token+type+timestamp) are rejected |
| `test_evaluate_condition_lt` | `"risk_score < 30"` evaluates correctly |
| `test_evaluate_condition_gt` | `"signal_count >= 3"` evaluates correctly |
| `test_evaluate_condition_none` | `None` condition always returns True |
| `test_check_chains_no_events` | No events = no chains started |
| `test_chain_starts_on_anchor` | Step 1 event creates an ActiveChain |
| `test_chain_advances` | Step 2 event within time window advances the chain |
| `test_chain_rejects_late_step` | Step 2 event outside time window is ignored |
| `test_chain_rejects_failed_condition` | Step with unmet condition is not matched |
| `test_chain_completes` | Min steps met triggers completion |
| `test_chain_completion_fires_event` | Completed chain emits chain_complete event |
| `test_chain_cooldown` | Same pattern+token cannot re-trigger within cooldown |
| `test_chain_expiry` | Chain past max_window is cleaned up |
| `test_pattern_hit_rate` | Hit rate computed correctly from chain_matches |
| `test_alert_formatting` | Chain completion alert has correct format |

### Integration test

A full chain scenario using `aioresponses` and `tmp_path` DB:
1. Emit `category_heating` event
2. Emit `laggard_picked` event 2h later
3. Emit `counter_scored` (risk_score=20) event 3h later
4. Verify chain completes with 3/4 steps
5. Verify conviction boost applied
6. Verify alert formatted correctly

---

## 13. Observability

### Dashboard endpoints (future)

```
GET /api/chains/active          # active chains in progress
GET /api/chains/completed       # completed chain history (last 7 days)
GET /api/chains/patterns        # pattern definitions with hit rates
GET /api/chains/events?token=X  # event timeline for a specific token
```

### Structured logging

All chain operations use structlog with consistent keys:

```python
logger.info("chain_started", token_id=token_id, pattern=pattern.name)
logger.info("chain_step_matched", token_id=token_id, pattern=pattern.name, step=step_num)
logger.info("chain_complete", token_id=token_id, pattern=pattern.name, 
            steps=steps_matched, duration_hours=duration)
logger.warning("chain_expired", token_id=token_id, pattern=pattern.name)
```

### Daily digest addition

Add a section to the existing daily digest:

```
Conviction Chains (24h):
  Completed: {count} ({patterns breakdown})
  In progress: {count}
  Pattern hit rates:
    full_conviction: {hit_rate}% ({triggers} triggers)
    narrative_momentum: {hit_rate}% ({triggers} triggers)
    volume_breakout: {hit_rate}% ({triggers} triggers)
```

---

## 14. Rollout Plan

### Phase 1: Event Store (Week 1)
- Implement `scout/chains/models.py` and `scout/chains/events.py`
- Add `signal_events` table to `scout/db.py`
- Add `emit_event()` calls to existing modules
- Write tests for event emission and storage
- Deploy: events accumulate silently, no user-facing changes

### Phase 2: Chain Tracker (Week 2)
- Implement `scout/chains/patterns.py` and `scout/chains/tracker.py`
- Add `chain_patterns`, `active_chains`, `chain_matches` tables
- Seed 3 built-in patterns with `alert_priority: "low"` (incubation)
- Write tracker tests
- Deploy: chains tracked silently, logged but no alerts

### Phase 3: Alerts + Scoring (Week 3)
- Implement `scout/chains/alerts.py`
- Wire conviction boost into gate.py
- Add chain section to daily digest
- After 2+ weeks of incubation data, promote patterns with good hit rates

### Phase 4: LEARN Integration (Week 4)
- Implement outcome tracking for chain_matches
- Compute per-pattern hit rates
- Implement pattern lifecycle (incubation → promotion → graduation → retirement)
- Dashboard endpoints (if dashboard exists)

---

## 15. What This Does NOT Do

- **No new API calls.** Operates entirely on data already being fetched and produced.
- **No Claude calls.** Pattern matching is deterministic. No LLM in the loop.
- **No changes to scoring weights.** The conviction boost is additive, applied after the existing scoring pipeline.
- **No hardcoded patterns.** All patterns are in the database table and configurable.
- **No blocking.** The tracker runs asynchronously and never blocks the main pipeline. If it crashes, the rest of the pipeline continues.
- **No pattern generation in v1.** The LEARN phase only evaluates existing patterns. Automatic pattern discovery is a future extension.

---

## 16. Open Questions

1. **Cross-pipeline token matching:** When a token appears in both the narrative pipeline (coin_id) and memecoin pipeline (contract_address), how do we reliably link them? CoinGecko coin detail has `platforms` with contract addresses, but we'd need an extra API call. For v1, chains are pipeline-scoped (narrative events only match narrative chains, memecoin events only match memecoin chains). Cross-pipeline matching is a v2 feature.

2. **Conviction boost stacking:** Can multiple completed chains boost the same token? For v1, yes — but total boost is capped at +30 points. A token that completes both `full_conviction` (+25) and `narrative_momentum` (+15) gets +30, not +40.

3. **Real-time vs. batch:** The 5-minute check interval means chains complete with up to 5 minutes of latency. This is fine for the current pipeline (30-min scan cycles). If the pipeline moves to real-time event streaming, the tracker could switch to event-driven processing.
