# Second-Wave / Cooldown Detection — Design Spec

**Date:** 2026-04-10
**Status:** Draft
**Goal:** Detect tokens that previously pumped, cooled down for 3-14 days, and are now showing early re-accumulation signals — catching "second wave" setups before the next move.
**Module:** `scout/secondwave/` — independent from existing pipeline and narrative agent
**Cost:** Near-zero incremental (uses existing DB data + 1-2 CoinGecko API calls per cycle)
**Deployment:** Runs inside gecko-alpha as a parallel async loop on Srilu VPS

---

## 1. Architecture Overview

```
┌────────────────────────────────────────────────────────────────┐
│                   SCAN (every 30 min)                           │
│  Query candidates + score_history for tokens that:              │
│    - Had quant_score >= 60 at some prior point                  │
│    - Were first_seen_at between 3-14 days ago                   │
│    - Haven't already been alerted as second-wave                │
│  Pure DB queries — zero API calls                               │
└──────────────────────┬─────────────────────────────────────────┘
                       ▼
┌────────────────────────────────────────────────────────────────┐
│                   CONFIRM (for scan hits)                       │
│  Batch-fetch current prices from CoinGecko /coins/markets      │
│  Compare current price to historical peak (from outcomes/       │
│  score_history) to detect drawdown + recovery pattern           │
│  Score re-accumulation strength                                 │
└──────────────────────┬─────────────────────────────────────────┘
                       ▼
┌────────────────────────────────────────────────────────────────┐
│                   STORE + ALERT                                 │
│  Insert into second_wave_candidates table                       │
│  Send Telegram alert with prior pump history + current signals  │
└────────────────────────────────────────────────────────────────┘
```

---

## 2. Module Structure

All new code lives in `scout/secondwave/`. No existing files are modified except `scout/db.py` (new table + queries), `scout/main.py` (add loop to gather), `scout/config.py` (new config keys), `dashboard/api.py` (new endpoint), and dashboard frontend (new section).

```
scout/secondwave/
  __init__.py
  detector.py      # Main detection logic: scan DB, filter, score re-accumulation
  models.py        # SecondWaveCandidate Pydantic model
  alerts.py        # Telegram alert formatting for second-wave candidates
```

---

## 3. Pydantic Models (`scout/secondwave/models.py`)

```python
from pydantic import BaseModel
from datetime import datetime


class SecondWaveCandidate(BaseModel):
    contract_address: str
    chain: str
    token_name: str
    ticker: str

    # Prior pump data (from candidates + score_history)
    peak_quant_score: int               # highest quant_score ever recorded
    peak_signals_fired: list[str]       # signals at peak score
    first_seen_at: datetime             # when pipeline first detected it
    original_alert_at: datetime | None  # when it was first alerted (if ever)
    original_market_cap: float          # market_cap at first detection
    peak_market_cap: float              # highest market_cap seen

    # Cooldown data
    days_since_first_seen: float        # age in days
    price_drop_from_peak_pct: float     # how far price fell from peak (negative %)

    # Re-accumulation signals (from fresh CoinGecko fetch)
    current_price: float
    current_market_cap: float
    current_volume_24h: float
    recovery_from_trough_pct: float     # how much price recovered from lowest point
    volume_vs_cooldown_avg: float       # current volume / avg volume during cooldown

    # Scoring
    reaccumulation_score: int           # 0-100 composite score
    reaccumulation_signals: list[str]   # which signals fired

    # Metadata
    detected_at: datetime
    alerted_at: datetime | None = None
```

---

## 4. Detection Logic (`scout/secondwave/detector.py`)

### Phase 1: DB Scan (zero API calls)

Query existing tables to find second-wave candidates:

```sql
SELECT c.contract_address, c.chain, c.token_name, c.ticker,
       c.quant_score, c.market_cap_usd, c.first_seen_at,
       c.signals_fired, c.alerted_at, c.volume_24h_usd
FROM candidates c
WHERE c.quant_score IS NOT NULL
  AND c.first_seen_at <= datetime('now', '-3 days')
  AND c.first_seen_at >= datetime('now', '-14 days')
  AND c.contract_address NOT IN (
      SELECT contract_address FROM second_wave_candidates
      WHERE detected_at >= datetime('now', '-7 days')
  )
```

Additionally, check `score_history` for each candidate to find their peak score:

```sql
SELECT MAX(score) as peak_score
FROM score_history
WHERE contract_address = ?
```

Filter: only proceed with candidates where `peak_score >= SECONDWAVE_MIN_PRIOR_SCORE` (default: 60).

### Phase 2: Fresh Price Confirmation (1-2 API calls)

For candidates passing the DB scan (typically 0-20 tokens), batch-fetch current prices from CoinGecko:

```
GET /coins/markets?vs_currency=usd&ids={comma_separated_ids}&per_page=250
```

This reuses the existing rate limiter from `scout/ingestion/coingecko.py`. Maximum 1-2 API calls per 30-min cycle (well within budget).

**Note:** Only tokens with `chain == "coingecko"` can be price-confirmed via CoinGecko. For DEX tokens (DexScreener/GeckoTerminal sourced), use the `outcomes` table `alert_price` and `check_price` as the price reference, and skip live price confirmation (the DB data is sufficient for detection).

### Phase 3: Re-accumulation Scoring

For each candidate with fresh price data, compute a re-accumulation score:

```python
def score_reaccumulation(
    candidate: dict,
    current_price: float,
    current_volume: float,
    current_market_cap: float,
    peak_market_cap: float,
    volume_history: list[float],   # from volume_snapshots
    settings: Settings,
) -> tuple[int, list[str]]:
    points = 0
    signals: list[str] = []

    # Signal 1: Drawdown from peak (must have dropped significantly)
    # Price must have dropped >30% from peak to qualify as "cooled down"
    if peak_market_cap > 0:
        drawdown_pct = ((current_market_cap - peak_market_cap) / peak_market_cap) * 100
        if drawdown_pct <= -settings.SECONDWAVE_MIN_DRAWDOWN_PCT:  # default: -30
            points += 25
            signals.append("sufficient_drawdown")

    # Signal 2: Recovery from trough (price stabilizing/recovering)
    # Must be up >5% from the lowest point during cooldown
    # Trough estimated from volume_snapshots correlation or min market_cap
    if recovery_from_trough_pct >= settings.SECONDWAVE_MIN_RECOVERY_PCT:  # default: 5.0
        points += 30
        signals.append("price_recovery")

    # Signal 3: Volume pickup vs cooldown average
    # Current volume must exceed average cooldown volume by threshold
    if len(volume_history) >= 3:
        cooldown_avg = sum(volume_history) / len(volume_history)
        if cooldown_avg > 0:
            vol_ratio = current_volume / cooldown_avg
            if vol_ratio >= settings.SECONDWAVE_VOL_PICKUP_RATIO:  # default: 2.0
                points += 25
                signals.append("volume_pickup")

    # Signal 4: Prior signal quality (strong first pump = stronger second wave)
    if candidate["peak_quant_score"] >= 75:
        points += 10
        signals.append("strong_prior_signal")

    # Signal 5: Holder retention (if holder snapshots available)
    # Holders staying flat or growing during cooldown = accumulation
    # Checked from holder_snapshots table
    if holder_retained:
        points += 10
        signals.append("holder_retention")

    # Normalize to 0-100
    points = min(100, points)
    return (points, signals)
```

### Phase 4: Threshold Gate

A candidate becomes a second-wave alert if:

```python
reaccumulation_score >= settings.SECONDWAVE_ALERT_THRESHOLD  # default: 50
```

This requires at least 2 of the 5 signals to fire (drawdown + recovery is the minimum viable pattern).

---

## 5. Database Schema

### `second_wave_candidates`

```sql
CREATE TABLE IF NOT EXISTS second_wave_candidates (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_address        TEXT NOT NULL,
    chain                   TEXT NOT NULL,
    token_name              TEXT NOT NULL,
    ticker                  TEXT NOT NULL,
    peak_quant_score        INTEGER NOT NULL,
    peak_signals_fired      TEXT,              -- JSON array
    first_seen_at           TEXT NOT NULL,
    original_alert_at       TEXT,
    original_market_cap     REAL,
    peak_market_cap         REAL,
    days_since_first_seen   REAL,
    price_drop_from_peak_pct REAL,
    current_price           REAL,
    current_market_cap      REAL,
    current_volume_24h      REAL,
    recovery_from_trough_pct REAL,
    volume_vs_cooldown_avg  REAL,
    reaccumulation_score    INTEGER NOT NULL,
    reaccumulation_signals  TEXT NOT NULL,      -- JSON array
    detected_at             TEXT NOT NULL,
    alerted_at              TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sw_contract
    ON second_wave_candidates(contract_address, detected_at);
CREATE INDEX IF NOT EXISTS idx_sw_score
    ON second_wave_candidates(reaccumulation_score);
```

### New DB methods (`scout/db.py` additions)

```python
async def get_secondwave_scan_candidates(
    self, min_age_days: int = 3, max_age_days: int = 14
) -> list[dict]:
    """Get candidates in the cooldown window that haven't been second-wave alerted."""

async def get_peak_score(self, contract_address: str) -> int | None:
    """Get the highest score ever recorded for a contract."""

async def was_secondwave_alerted(
    self, contract_address: str, days: int = 7
) -> bool:
    """Check if a contract was already flagged as second-wave recently."""

async def insert_secondwave_candidate(self, candidate: dict) -> None:
    """Store a second-wave detection."""

async def get_recent_secondwave_candidates(
    self, days: int = 7
) -> list[dict]:
    """Get second-wave candidates from the last N days for dashboard."""

async def get_volume_history(
    self, contract_address: str, days: int = 14
) -> list[float]:
    """Get volume snapshots for a contract over the cooldown window."""
```

---

## 6. Configuration (`scout/config.py` additions)

```python
# Second-Wave Detection
SECONDWAVE_ENABLED: bool = False                     # opt-in, disabled by default
SECONDWAVE_POLL_INTERVAL: int = 1800                 # 30 min scan cycle
SECONDWAVE_MIN_PRIOR_SCORE: int = 60                 # min peak quant_score to consider
SECONDWAVE_COOLDOWN_MIN_DAYS: int = 3                # min days since first detection
SECONDWAVE_COOLDOWN_MAX_DAYS: int = 14               # max days (older = stale)
SECONDWAVE_MIN_DRAWDOWN_PCT: float = 30.0            # min % drop from peak to qualify
SECONDWAVE_MIN_RECOVERY_PCT: float = 5.0             # min % recovery from trough
SECONDWAVE_VOL_PICKUP_RATIO: float = 2.0             # current vol / cooldown avg vol
SECONDWAVE_ALERT_THRESHOLD: int = 50                 # min reaccumulation_score to alert
SECONDWAVE_DEDUP_DAYS: int = 7                       # don't re-alert same token within N days
```

Feature is disabled by default. Enable with `SECONDWAVE_ENABLED=true` in `.env`.

---

## 7. Alert Format (`scout/secondwave/alerts.py`)

### Telegram alert

```
🔄 Second Wave Detected: {token_name} ({ticker})

Prior pump (first seen {days_since_first_seen}d ago):
  Peak score: {peak_quant_score}/100
  Signals: {peak_signals_fired}
  Peak market cap: ${peak_market_cap:,.0f}

Cooldown:
  Drawdown from peak: {price_drop_from_peak_pct:.1f}%
  Days cooling: {days_since_first_seen:.0f}

Re-accumulation:
  Recovery from trough: +{recovery_from_trough_pct:.1f}%
  Volume vs cooldown avg: {volume_vs_cooldown_avg:.1f}x
  Re-accumulation score: {reaccumulation_score}/100
  Signals: {reaccumulation_signals}

Current: ${current_market_cap:,.0f} mcap | ${current_volume_24h:,.0f} vol/24h

{source_url}

⚠️ RESEARCH ONLY - Not financial advice
```

Uses existing `scout/alerter.py` `send_telegram_message()` for delivery. The `format_secondwave_alert()` function lives in `scout/secondwave/alerts.py`.

---

## 8. Main Loop Integration (`scout/main.py`)

```python
async def main():
    settings = Settings()

    async with aiohttp.ClientSession() as session:
        tasks = [pipeline_loop(session, settings)]

        if settings.NARRATIVE_ENABLED:
            tasks.append(narrative_agent_loop(session, settings))

        if settings.SECONDWAVE_ENABLED:
            tasks.append(secondwave_loop(session, settings))

        await asyncio.gather(*tasks)


async def secondwave_loop(session: aiohttp.ClientSession, settings: Settings):
    """Second-wave detection loop.

    Runs every SECONDWAVE_POLL_INTERVAL seconds. Scans existing DB data
    for tokens in cooldown window, fetches fresh prices to confirm
    re-accumulation, and alerts on qualifying candidates.
    """
    db = Database(settings.DB_PATH)
    await db.initialize()

    while True:
        try:
            # Phase 1: DB scan
            scan_candidates = await db.get_secondwave_scan_candidates(
                min_age_days=settings.SECONDWAVE_COOLDOWN_MIN_DAYS,
                max_age_days=settings.SECONDWAVE_COOLDOWN_MAX_DAYS,
            )

            if scan_candidates:
                # Phase 2: Fetch fresh prices (batch)
                cg_ids = [c["contract_address"] for c in scan_candidates
                          if c["chain"] == "coingecko"]
                fresh_prices = await fetch_current_prices(session, cg_ids, settings)

                # Phase 3: Score each candidate
                for candidate in scan_candidates:
                    peak_score = await db.get_peak_score(candidate["contract_address"])
                    if peak_score is None or peak_score < settings.SECONDWAVE_MIN_PRIOR_SCORE:
                        continue

                    volume_history = await db.get_volume_history(
                        candidate["contract_address"],
                        days=settings.SECONDWAVE_COOLDOWN_MAX_DAYS,
                    )

                    score, signals = score_reaccumulation(
                        candidate, fresh_prices, volume_history, settings
                    )

                    # Phase 4: Gate + alert
                    if score >= settings.SECONDWAVE_ALERT_THRESHOLD:
                        sw_candidate = build_secondwave_candidate(
                            candidate, peak_score, score, signals, fresh_prices
                        )
                        await db.insert_secondwave_candidate(sw_candidate)
                        alert_msg = format_secondwave_alert(sw_candidate)
                        await send_telegram_message(alert_msg, session, settings)

        except Exception as e:
            logger.error("secondwave_loop_error", error=str(e))

        await asyncio.sleep(settings.SECONDWAVE_POLL_INTERVAL)
```

---

## 9. Dashboard Integration

### API Endpoints (`dashboard/api.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/secondwave/candidates` | Recent second-wave candidates, paginated |
| GET | `/api/secondwave/stats` | Summary stats: count, avg score, detection rate |

### Dashboard Section

A new "Second Wave" card/section on the dashboard showing:

1. **Active Second-Wave Candidates** — table with: token name, peak prior score, days cooling, drawdown %, recovery %, re-accumulation score, detected time. Sorted by reaccumulation_score descending.

2. **Prior Pump Context** — expandable row detail showing the token's original alert history, signals fired at peak, and price trajectory (from score_history/volume_snapshots).

3. **Detection Rate** — simple stat: "X second-wave candidates detected in last 7 days" with average reaccumulation_score.

This is intentionally minimal. The dashboard is informational, not a control panel (unlike the narrative agent which has strategy overrides).

---

## 10. Rate Limiting & API Budget

The second-wave detector is designed to be extremely lightweight on API calls:

| Phase | API Calls | When |
|-------|-----------|------|
| DB Scan | 0 | Every 30 min |
| Price Confirm | 1-2 | Only when DB scan finds candidates |
| Total per hour | 0-4 | Typically 0-2 |

All CoinGecko calls share the existing rate limiter from `scout/ingestion/coingecko.py` (30 req/min free tier). The second-wave detector adds negligible load.

For DEX-sourced tokens (DexScreener, GeckoTerminal), no API calls are made. Detection relies entirely on DB data (score_history, volume_snapshots, outcomes). A future enhancement could add DexScreener price confirmation, but it is not needed for v1.

---

## 11. Testing Strategy

| Test File | Coverage |
|-----------|----------|
| `tests/test_secondwave_detector.py` | DB scan query, peak score lookup, reaccumulation scoring math, threshold gating, dedup logic |
| `tests/test_secondwave_alerts.py` | Alert message formatting, truncation, edge cases (missing fields) |
| `tests/test_secondwave_models.py` | Pydantic model validation, serialization |
| `tests/test_secondwave_db.py` | Table creation, insert, query, dedup check, volume history retrieval |

### Key test scenarios

- **Happy path:** Token with quant_score=75 seen 5 days ago, market cap dropped 40% from peak, now recovering 8% with 3x volume pickup. Should score >= 50 and trigger alert.
- **Too fresh:** Token first_seen_at 1 day ago. Should be excluded by cooldown window.
- **Too stale:** Token first_seen_at 20 days ago. Should be excluded.
- **No drawdown:** Token never dropped significantly. Should not qualify (no `sufficient_drawdown` signal).
- **Weak prior signal:** Token peak_quant_score=30. Should be excluded by `SECONDWAVE_MIN_PRIOR_SCORE`.
- **Already alerted:** Token was second-wave alerted 3 days ago. Should be deduped.
- **No volume history:** Token has < 3 volume snapshots. `volume_pickup` signal should not fire but other signals can still qualify the candidate.
- **DEX token (no CoinGecko ID):** Should use DB-only data, skip live price confirmation.

Mock strategy: `aioresponses` for CoinGecko HTTP mocks, `tmp_path` for DB fixtures. Same patterns as existing test suite.

---

## 12. Out of Scope

- **On-chain analysis** — no wallet/holder clustering, just aggregate holder_count from existing snapshots
- **Automated position sizing** — detection only, not trading signals
- **Cross-token correlation** — does not detect sector-wide second waves (that is narrative agent territory)
- **DexScreener/GeckoTerminal live price confirmation** — v1 uses CoinGecko only for live price; DEX tokens use DB data
- **Self-learning** — no strategy adjustment loop (unlike narrative agent); thresholds are static config. Can be added later if detection rate warrants it
- **MiroFish/narrative scoring** — second-wave candidates are not run through narrative simulation. The prior pump already validated the narrative

---

## 13. Known Limitations & Implementation Notes

1. **Price history resolution:** The detector relies on `volume_snapshots` and `score_history` for historical data. If the main pipeline didn't scan a token during its cooldown (it wouldn't, since it aged out), the volume history during cooldown will be sparse. The `volume_pickup` signal may not fire for many candidates. This is acceptable for v1 — the `sufficient_drawdown` + `price_recovery` signals from the fresh CoinGecko fetch are the primary indicators.

2. **Peak market cap estimation:** The `candidates` table stores `market_cap_usd` at the most recent scan, not the historical peak. For peak estimation, use the `outcomes` table `alert_price` (if alerted) or the `score_history` peak score timestamp to approximate when the token was hottest. A future enhancement could add a `market_cap_snapshots` table.

3. **CoinGecko ID mapping:** Tokens sourced from DexScreener/GeckoTerminal use contract addresses, not CoinGecko IDs. The `/coins/markets?ids=` endpoint requires CoinGecko IDs. For DEX tokens, skip live price confirmation and rely on DB data only. This is documented in the detection logic.

4. **Cooldown window vs pruning:** The `candidates` table is pruned at 7 days by default (`prune_old_candidates`). To detect second waves in the 7-14 day window, either increase `keep_days` or query `score_history` and `alerts` tables which are not pruned. The implementation should primarily use `score_history` + `alerts` for the scan query, not the `candidates` table directly.

5. **False positives from dead coins:** A coin that dropped 90% and bounced 5% off the bottom is technically meeting the drawdown + recovery criteria but may be a dead cat bounce. The `volume_pickup` and `holder_retention` signals help filter these, but some false positives are expected. The 50-point alert threshold requiring 2+ signals mitigates this.

6. **Candidate pruning retention adjustment:** The default `prune_old_candidates(keep_days=7)` would delete candidates before they enter the 7-14 day cooldown window. Implementation must ensure `score_history`, `alerts`, and `volume_snapshots` data persists for at least `SECONDWAVE_COOLDOWN_MAX_DAYS` (14 days). The candidates table can still prune at 7 days since the scan should query `score_history` and `alerts` as primary data sources.
