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
│  Query alerts + score_history for tokens that:                  │
│    - Were alerted 3-14 days ago (from alerts.alerted_at)        │
│    - Had peak quant_score >= 60 (from score_history)            │
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

    # Prior pump data (from alerts + score_history)
    coingecko_id: str | None = None     # CoinGecko slug (from predictions.coin_id for narrative tokens)
    peak_quant_score: int               # highest quant_score ever recorded
    peak_signals_fired: list[str]       # signals at peak score
    first_seen_at: datetime             # when pipeline first detected it
    original_alert_at: datetime | None  # when it was first alerted (if ever)
    original_market_cap: float          # market_cap at first detection
    alert_market_cap: float             # market_cap at alert time (from alerts table, approximate peak)

    # Cooldown data
    days_since_first_seen: float        # age in days
    price_drop_from_peak_pct: float     # how far price fell from peak (negative %)

    # Re-accumulation signals (from fresh CoinGecko fetch)
    current_price: float
    current_market_cap: float
    current_volume_24h: float
    price_vs_alert_pct: float           # current_price / alert_price as % (>70% = recovery)
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

Query `alerts` table (persists beyond the 7-day `candidates` prune window) joined with `score_history` to find second-wave candidates in the full 3-14 day cooldown window:

```sql
SELECT a.contract_address, a.chain, a.token_name, a.ticker,
       a.market_cap_usd AS alert_market_cap, a.alerted_at,
       a.price_usd AS alert_price,
       p.coin_id AS coingecko_id,
       MAX(sh.score) AS peak_score
FROM alerts a
LEFT JOIN score_history sh ON sh.contract_address = a.contract_address
LEFT JOIN predictions p ON p.contract_address = a.contract_address
WHERE a.alerted_at <= datetime('now', '-3 days')
  AND a.alerted_at >= datetime('now', '-14 days')
  AND a.contract_address NOT IN (
      SELECT contract_address FROM second_wave_candidates
      WHERE detected_at >= datetime('now', '-7 days')
  )
GROUP BY a.contract_address
HAVING peak_score >= 60   -- SECONDWAVE_MIN_PRIOR_SCORE
```

The `alerts` table has `alerted_at` and `contract_address` and is not subject to the 7-day candidate pruning, giving us the full 3-14 day window. The `LEFT JOIN predictions` provides `coin_id` (CoinGecko slug) for narrative agent tokens; DEX-sourced tokens will have `coingecko_id = NULL`.

Filter: only proceed with candidates where `peak_score >= SECONDWAVE_MIN_PRIOR_SCORE` (default: 60). The peak score filter is applied directly in the SQL `HAVING` clause (M1).

### Phase 2: Fresh Price Confirmation (1-2 API calls)

For candidates passing the DB scan (typically 0-20 tokens), batch-fetch current prices from CoinGecko.

**Narrative agent tokens** (have `coingecko_id` from `predictions.coin_id`): fetch live prices via:

```
GET /coins/markets?vs_currency=usd&ids={comma_separated_coingecko_ids}&per_page=250
```

This reuses the existing rate limiter from `scout/ingestion/coingecko.py`. Maximum 1-2 API calls per 30-min cycle (well within budget).

**DEX-sourced tokens** (have `coingecko_id = NULL`): contract_address cannot be reliably mapped to a CoinGecko slug. For these tokens, use `alert_price` from the `alerts` table as the last known price reference, marked as `"stale_price"` in the result. The `price_vs_alert_pct` field is computed as `stale_price / alert_price * 100` (which will be ~100% and thus not trigger recovery signals — this is intentional; DEX tokens rely on `sufficient_drawdown` and `strong_prior_signal` only).

### Phase 3: Re-accumulation Scoring

For each candidate with fresh price data, compute a re-accumulation score:

```python
def score_reaccumulation(
    candidate: dict,
    current_price: float | None,     # None for DEX tokens with stale price
    current_volume: float | None,
    current_market_cap: float | None,
    alert_market_cap: float,          # from alerts table (approximate peak)
    alert_price: float,               # from alerts table
    volume_history: list[float],      # from volume_snapshots (may be empty)
    settings: Settings,
) -> tuple[int, list[str]]:
    points = 0
    signals: list[str] = []

    # Signal 1: Drawdown from peak (must have dropped significantly)  [30 pts]
    # Price must have dropped >30% from alert-time market cap to qualify as "cooled down"
    if alert_market_cap > 0 and current_market_cap is not None:
        drawdown_pct = ((current_market_cap - alert_market_cap) / alert_market_cap) * 100
        if drawdown_pct <= -settings.SECONDWAVE_MIN_DRAWDOWN_PCT:  # default: -30
            points += 30
            signals.append("sufficient_drawdown")

    # Signal 2: Price recovery vs alert price  [35 pts]
    # Concrete formula: if current_price > alert_price * 0.7 (within 30% of alert price),
    # the token has recovered from its trough. This replaces the undefined
    # recovery_from_trough_pct variable with a direct current_price vs alert_price check.
    # price_vs_alert_pct = (current_price / alert_price) * 100
    if current_price is not None and alert_price > 0:
        price_vs_alert_pct = (current_price / alert_price) * 100
        if price_vs_alert_pct >= settings.SECONDWAVE_MIN_RECOVERY_PCT:  # default: 70.0
            points += 35
            signals.append("price_recovery")

    # Signal 3: Volume pickup vs cooldown average  [20 pts]
    # Current volume must exceed average cooldown volume by threshold.
    # NOTE: volume_snapshots will often be empty during cooldown because the main
    # pipeline stops scanning tokens after they age out. When data is unavailable
    # (< 3 snapshots), this signal scores 0 — it does not crash.
    if current_volume is not None and len(volume_history) >= 3:
        cooldown_avg = sum(volume_history) / len(volume_history)
        if cooldown_avg > 0:
            vol_ratio = current_volume / cooldown_avg
            if vol_ratio >= settings.SECONDWAVE_VOL_PICKUP_RATIO:  # default: 2.0
                points += 20
                signals.append("volume_pickup")

    # Signal 4: Prior signal quality (strong first pump = stronger second wave)  [15 pts]
    if candidate["peak_quant_score"] >= 75:
        points += 15
        signals.append("strong_prior_signal")

    # NOTE: Holder retention signal removed from v1. Holder data (from Helius/Moralis)
    # is not reliably available during the cooldown window. This can be added in v2
    # when Helius/Moralis integration lands, at which point it would be worth ~10 pts
    # (deducted proportionally from the other 4 signals).

    # Signals sum to 100: 30 + 35 + 20 + 15 = 100
    points = min(100, points)
    return (points, signals)
```

### Phase 4: Threshold Gate

A candidate becomes a second-wave alert if:

```python
reaccumulation_score >= settings.SECONDWAVE_ALERT_THRESHOLD  # default: 50
```

With 4 signals (drawdown=30, recovery=35, volume=20, prior_quality=15), this requires at least 2 of the 4 signals to fire. The minimum viable pattern is `sufficient_drawdown` (30) + `strong_prior_signal` (15) = 45, which is below threshold, so `price_recovery` (35) must be one of the two signals fired. Note: `volume_pickup` may score 0 when volume snapshot data is unavailable during cooldown — this is expected and does not block detection.

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
    coingecko_id            TEXT,              -- CoinGecko slug (NULL for DEX tokens)
    peak_quant_score        INTEGER NOT NULL,
    peak_signals_fired      TEXT,              -- JSON array
    first_seen_at           TEXT NOT NULL,
    original_alert_at       TEXT,
    original_market_cap     REAL,
    alert_market_cap        REAL,              -- from alerts.market_cap_usd (approximate peak)
    days_since_first_seen   REAL,
    price_drop_from_peak_pct REAL,
    current_price           REAL,
    current_market_cap      REAL,
    current_volume_24h      REAL,
    price_vs_alert_pct      REAL,              -- current_price / alert_price * 100
    volume_vs_cooldown_avg  REAL,
    price_is_stale          INTEGER NOT NULL DEFAULT 0,  -- 1 if DEX token w/ no live price
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
    self, min_age_days: int = 3, max_age_days: int = 14,
    min_peak_score: int = 60,
) -> list[dict]:
    """Get alerted tokens in the cooldown window that haven't been second-wave
    alerted. Queries the `alerts` table (persists beyond candidates prune window)
    joined with `score_history` (peak score) and `predictions` (coingecko_id).
    The peak score filter is applied in the SQL HAVING clause — no separate
    get_peak_score() call is needed (M1)."""

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
SECONDWAVE_MIN_RECOVERY_PCT: float = 70.0             # min price_vs_alert_pct (current/alert * 100) to count as recovery
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
  Alert market cap: ${alert_market_cap:,.0f}  (approximate peak)

Cooldown:
  Drawdown from peak: {price_drop_from_peak_pct:.1f}%
  Days cooling: {days_since_first_seen:.0f}

Re-accumulation:
  Price vs alert: {price_vs_alert_pct:.1f}% {stale_marker}
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
            # Phase 1: DB scan — peak_score is computed in-SQL via MAX(sh.score)
            # and filtered via HAVING, so no separate get_peak_score() call (M1).
            scan_candidates = await db.get_secondwave_scan_candidates(
                min_age_days=settings.SECONDWAVE_COOLDOWN_MIN_DAYS,
                max_age_days=settings.SECONDWAVE_COOLDOWN_MAX_DAYS,
                min_peak_score=settings.SECONDWAVE_MIN_PRIOR_SCORE,
            )

            if scan_candidates:
                # Phase 2: Fetch fresh prices (batch) — only for tokens with
                # a resolved coingecko_id (H1). DEX tokens fall back to
                # alert_price as a stale reference.
                cg_ids = [c["coingecko_id"] for c in scan_candidates
                          if c.get("coingecko_id")]
                fresh_prices = (
                    await fetch_current_prices(session, cg_ids, settings)
                    if cg_ids else {}
                )

                # Phase 3: Score each candidate
                for candidate in scan_candidates:
                    volume_history = await db.get_volume_history(
                        candidate["contract_address"],
                        days=settings.SECONDWAVE_COOLDOWN_MAX_DAYS,
                    )

                    cg_id = candidate.get("coingecko_id")
                    if cg_id and cg_id in fresh_prices:
                        price_data = fresh_prices[cg_id]
                        current_price = price_data["current_price"]
                        current_volume = price_data["total_volume"]
                        current_market_cap = price_data["market_cap"]
                        price_is_stale = False
                    else:
                        # DEX token: no live price, use alert_price as stale reference
                        current_price = candidate["alert_price"]
                        current_volume = None
                        current_market_cap = candidate["alert_market_cap"]
                        price_is_stale = True

                    score, signals = score_reaccumulation(
                        candidate,
                        current_price=current_price,
                        current_volume=current_volume,
                        current_market_cap=current_market_cap,
                        alert_market_cap=candidate["alert_market_cap"],
                        alert_price=candidate["alert_price"],
                        volume_history=volume_history,
                        settings=settings,
                    )

                    # Phase 4: Gate + alert
                    if score >= settings.SECONDWAVE_ALERT_THRESHOLD:
                        sw_candidate = build_secondwave_candidate(
                            candidate, score, signals,
                            current_price, current_volume, current_market_cap,
                            price_is_stale,
                        )
                        await db.insert_secondwave_candidate(sw_candidate)
                        alert_msg = format_secondwave_alert(sw_candidate)
                        await send_telegram_message(alert_msg, session, settings)

        except Exception:
            # Use logger.exception to capture full traceback (m1)
            logger.exception("secondwave_loop_error")

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

- **Happy path (narrative token):** Token with peak quant_score=75, alerted 5 days ago, market cap dropped 40% from alert_market_cap, current price = 80% of alert_price (price_recovery fires), 3x volume pickup. Scores 30+35+20+15=100 and triggers alert.
- **DEX token (stale price):** Token with no coingecko_id, peak quant_score=78, alerted 6 days ago, alert_market_cap=$2M, current DB price data shows $1.2M. Scores sufficient_drawdown (30) + strong_prior_signal (15) = 45, below threshold — correctly filtered.
- **Too fresh:** Token first_seen_at 1 day ago. Should be excluded by cooldown window.
- **Too stale:** Token first_seen_at 20 days ago. Should be excluded.
- **No drawdown:** Token never dropped significantly. Should not qualify (no `sufficient_drawdown` signal).
- **Weak prior signal:** Token peak_quant_score=30. Should be excluded by `SECONDWAVE_MIN_PRIOR_SCORE`.
- **Already alerted:** Token was second-wave alerted 3 days ago. Should be deduped.
- **No volume history:** Token has < 3 volume snapshots. `volume_pickup` signal should score 0 (not crash); other signals can still qualify the candidate.
- **DEX token (no CoinGecko ID):** Should skip live price confirmation, use `alerts.price_usd` as stale reference, mark `price_is_stale=True`.

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

2. **Peak market cap estimation (H2):** The `alerts` table stores `market_cap_usd` at alert time, which is used as the approximate peak in the `alert_market_cap` field. This is explicitly an approximation — the token may have pumped further after the alert, but we do not currently snapshot the post-alert peak. If `alert_market_cap` is NULL for a given alert row, `price_drop_from_peak_pct` is reported as `None` and the `sufficient_drawdown` signal simply does not fire. A future enhancement could add a `market_cap_snapshots` table to capture true post-alert peaks.

3. **CoinGecko ID mapping (H1):** Tokens sourced from DexScreener/GeckoTerminal use contract addresses, not CoinGecko IDs. The `/coins/markets?ids=` endpoint requires CoinGecko IDs (slugs). Narrative agent tokens have `coin_id` in the `predictions` table and can be price-confirmed live. DEX tokens without a mapped slug fall back to the `alerts.price_usd` as a stale reference, flagged `price_is_stale=True` in the output row and the alert message. For these tokens, `price_recovery` will not fire (stale price ratio is ~100%), so they must qualify via `sufficient_drawdown` + `strong_prior_signal` + (optionally) `volume_pickup`.

4. **Cooldown window vs pruning (B1):** The `candidates` table is pruned at 7 days by default (`prune_old_candidates`), which would delete rows before they enter the 7-14 day cooldown window. The scan query therefore uses the `alerts` table (which has `alerted_at` + `contract_address` and is not pruned) joined with `score_history` (historical peak scores, also not pruned). Implementation must ensure `alerts`, `score_history`, and `volume_snapshots` data persists for at least `SECONDWAVE_COOLDOWN_MAX_DAYS` (14 days). The `candidates` table can continue pruning at 7 days since it is no longer the primary data source.

5. **False positives from dead coins:** A coin that dropped 90% and bounced slightly off the bottom is technically meeting the drawdown + recovery criteria but may be a dead cat bounce. The `volume_pickup` and `strong_prior_signal` signals help filter these, but some false positives are expected. The 50-point alert threshold requiring 2+ signals mitigates this.

6. **Holder retention deferred to v2 (B3):** The initial design included a `holder_retention` signal, but holder data (from Helius/Moralis) is not reliably available during the cooldown window in v1. That signal has been removed; the remaining 4 signals (drawdown 30, recovery 35, volume 20, prior quality 15) sum to 100. Holder retention can be re-added in v2 when the Helius/Moralis integration lands.

7. **Volume snapshots sparse during cooldown (H3):** The main pipeline stops scanning tokens after they age out, so `volume_snapshots` will typically have few or zero entries during the 3-14 day cooldown window. The `volume_pickup` signal gracefully scores 0 when fewer than 3 snapshots are available (it does not raise). Detection still proceeds on the other 3 signals. The threshold analysis assumes `volume_pickup` frequently does not fire.
