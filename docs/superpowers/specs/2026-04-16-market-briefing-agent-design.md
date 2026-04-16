# Market Briefing Agent — Design Spec

**Date:** 2026-04-16
**Status:** Approved
**Goal:** Automated 12-hourly market intelligence briefing that condenses hours of crypto research into a structured, actionable report. Delivered via Telegram + dashboard tab.
**Module:** `scout/briefing/` — independent, collects from free APIs + internal DB
**Cost:** ~$9/month (Claude Sonnet synthesis, 2x daily)

---

## 1. Architecture Overview

```
Every 12 hours (configurable, default 6am + 6pm UTC):

  Phase 1: COLLECT (deterministic, parallel, ~10 seconds)
  ├── Fear & Greed Index        → market sentiment
  ├── CoinGecko /global         → total mcap, BTC dominance, 24h change
  ├── CoinGlass funding rates   → BTC/ETH funding, liquidations
  ├── DeFi Llama TVL            → chain-level TVL flows
  ├── CryptoCompare news        → top 10 crypto headlines
  └── Internal DB queries       → heating categories, early catches,
                                   predictions, paper trades, chain matches

  Phase 2: SYNTHESIZE (one Claude Sonnet call, ~$0.15)
  ├── All raw data passed as structured JSON
  ├── Prompt: senior crypto analyst briefing format
  └── Output: formatted sections with 📌 actionable insights

  Phase 3: DELIVER
  ├── Store in briefings table (raw + synthesis)
  ├── Send to Telegram (if enabled)
  └── Display on Briefing dashboard tab
```

---

## 2. Module Structure

```
scout/briefing/
  __init__.py
  collector.py     # Parallel data collection from free APIs + internal DB
  synthesizer.py   # Claude Sonnet synthesis — raw data → analyst briefing
  models.py        # BriefingData, BriefingSection models
```

---

## 3. Data Sources (All Free)

### External APIs

| Source | URL | Data Collected | Rate Limit |
|--------|-----|---------------|------------|
| **Fear & Greed Index** | `api.alternative.me/fng/?limit=2` | Current value + classification + yesterday's value | Free, no key |
| **CoinGecko Global** | `api.coingecko.com/api/v3/global` | total_market_cap, market_cap_change_24h, btc_dominance, eth_dominance, active_cryptocurrencies | Uses shared rate limiter |
| **CoinGlass Funding** | `open-api.coinglass.com/public/v2/funding` | BTC/ETH funding rates across exchanges | Free public endpoint |
| **CoinGlass Liquidations** | `open-api.coinglass.com/public/v2/liquidation_history` | 24h liquidation totals, long vs short | Free public endpoint |
| **DeFi Llama Chains** | `api.llama.fi/v2/chains` | TVL per chain, 1d change | Free, no key, no rate limit |
| **CryptoCompare News** | `min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=popular` | Top 10 headlines with title, source, url, categories | Free, no key |

### Internal DB Queries

| Query | Source Table | Data |
|-------|-------------|------|
| Heating categories | `narrative_signals` | Top 5 heating + top 5 cooling categories (last 12h) |
| Early catches | `trending_comparisons` | Tokens caught before trending (last 12h), with lead time + peak gain |
| Top gainers caught | `gainers_comparisons` | Gainers we detected early |
| Narrative predictions | `predictions` | Recent Claude-scored picks with outcomes |
| Paper trading PnL | `paper_trades` | Open positions summary, signal-type breakdown |
| Market regime | `category_snapshots` | Current BULL/BEAR/CRAB from latest snapshot |
| Volume spikes | `volume_spikes` | Recent volume anomalies |
| Chain matches | `chain_matches` | Recent conviction chain completions |

---

## 4. Collector (`collector.py`)

```python
async def collect_briefing_data(session: aiohttp.ClientSession, db: Database, settings) -> dict:
    """Collect all data sources in parallel. Returns structured dict."""

    # External API calls (parallel via asyncio.gather)
    fear_greed, cg_global, funding, liquidations, tvl, news = await asyncio.gather(
        fetch_fear_greed(session),
        fetch_cg_global(session, settings.COINGECKO_API_KEY),
        fetch_funding_rates(session),
        fetch_liquidations(session),
        fetch_defi_tvl(session),
        fetch_crypto_news(session),
        return_exceptions=True,
    )

    # Internal DB queries (sequential, fast)
    internal = await collect_internal_data(db)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fear_greed": fear_greed if not isinstance(fear_greed, Exception) else None,
        "global_market": cg_global if not isinstance(cg_global, Exception) else None,
        "funding_rates": funding if not isinstance(funding, Exception) else None,
        "liquidations": liquidations if not isinstance(liquidations, Exception) else None,
        "defi_tvl": tvl if not isinstance(tvl, Exception) else None,
        "news": news if not isinstance(news, Exception) else None,
        "internal": internal,
    }
```

### Individual Fetch Functions

Each returns a dict or raises on failure (caught by gather):

```python
async def fetch_fear_greed(session) -> dict:
    """Returns {"value": 72, "classification": "Greed", "previous": 65}"""

async def fetch_cg_global(session, api_key="") -> dict:
    """Returns {"total_mcap": 2.5e12, "mcap_change_24h": 3.4,
                "btc_dominance": 56.9, "eth_dominance": 10.2}"""

async def fetch_funding_rates(session) -> dict:
    """Returns {"btc": 0.01, "eth": 0.03}"""

async def fetch_liquidations(session) -> dict:
    """Returns {"total_24h": 142_000_000, "long_pct": 35, "short_pct": 65}"""

async def fetch_defi_tvl(session) -> dict:
    """Returns {"total": 89.2e9, "change_1d_pct": 1.2,
                "top_chains": [{"name": "Ethereum", "tvl": 45e9, "change": 0.8}, ...]}"""

async def fetch_crypto_news(session) -> list[dict]:
    """Returns top 10: [{"title": "...", "source": "...", "url": "...", "categories": "..."}]"""
```

### Internal Data Collection

```python
async def collect_internal_data(db) -> dict:
    """Query our own DB for system intelligence."""
    conn = db._conn
    return {
        "market_regime": await _get_current_regime(conn),
        "heating_categories": await _get_heating_categories(conn, hours=12),
        "cooling_categories": await _get_cooling_categories(conn, hours=12),
        "early_catches": await _get_recent_catches(conn, hours=12),
        "predictions": await _get_recent_predictions(conn, hours=12),
        "paper_pnl": await _get_paper_summary(conn),
        "volume_spikes": await _get_recent_spikes(conn, hours=12),
        "chain_completions": await _get_recent_chains(conn, hours=12),
    }
```

### Error Handling

Each external fetch is wrapped in asyncio.gather with `return_exceptions=True`. Failed sources return None in the briefing data — the synthesizer handles missing sections gracefully. No single API failure blocks the briefing.

All external calls use `aiohttp.ClientTimeout(total=15)`. CoinGecko call uses the shared rate limiter.

---

## 5. Synthesizer (`synthesizer.py`)

One Claude Sonnet call transforms raw data into an analyst-grade briefing.

### System Prompt

```
You are a senior crypto market analyst preparing a structured briefing for a trader.
Your job: synthesize raw market data into actionable intelligence.

Rules:
- Start each section with a 📌 insight that connects the data to a trading thesis
- Be specific with numbers — don't say "up significantly", say "+18.2%"
- Connect dots across sections — if BTC dominance is falling AND alt categories are heating, say so
- Flag contradictions — if sentiment is "Greed" but funding is negative, note the divergence
- Keep each section to 3-5 bullet points max
- End with 1-2 sentences: "Bottom line: [market stance]"
- Use these exact section headers and emoji
```

### User Prompt Template

```
Generate a market briefing from the following data collected at {timestamp}.

=== RAW DATA ===
{json.dumps(briefing_data, indent=2)}

=== REQUIRED SECTIONS ===

🔍 GECKO-ALPHA MARKET BRIEFING — {date_formatted}

📊 MACRO PULSE
- Fear & Greed index, direction, what it signals
- Total market cap + 24h change
- BTC dominance + trend implication
- 📌 One key macro insight connecting these

📈 BTC & ETH
- BTC price, 24h change, key data point (ETF flows, exchange flows)
- ETH price, 24h change, key data point (staking, L2 activity)
- 📌 What BTC+ETH behavior signals for the broader market

🔥 SECTOR ROTATION
- Top 3 heating categories with acceleration %
- Top 3 cooling categories
- 📌 Which narrative rotation to watch and why

⛓️ ON-CHAIN SIGNALS
- Funding rates (bullish/bearish/neutral interpretation)
- Liquidation data (who's getting liquidated, what it means)
- DeFi TVL trend
- 📌 On-chain conviction signal

📰 NEWS & CATALYSTS
- Top 3-5 most impactful headlines
- 📌 Which news is most likely to move markets in the next 12h

🎯 OUR EARLY CATCHES
- Tokens we detected before trending (with lead time + peak gain)
- Active narrative predictions
- 📌 Our system's current edge

📊 PAPER TRADING SNAPSHOT
- Open positions + unrealized PnL
- By signal type performance
- 📌 Which signal types are producing alpha

💡 BOTTOM LINE
- 1-2 sentence market stance
- Key levels or events to watch in next 12h

Return ONLY the formatted briefing text. No JSON, no code blocks.
```

### Synthesis Function

```python
async def synthesize_briefing(raw_data: dict, api_key: str, model: str) -> str:
    """Call Claude Sonnet to synthesize raw data into formatted briefing."""
    client = anthropic.AsyncAnthropic(api_key=api_key)
    message = await client.messages.create(
        model=model,
        max_tokens=2000,
        temperature=0.3,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": format_user_prompt(raw_data)}],
    )
    return message.content[0].text
```

---

## 6. Models (`models.py`)

```python
class BriefingData(BaseModel):
    timestamp: datetime
    fear_greed: dict | None = None
    global_market: dict | None = None
    funding_rates: dict | None = None
    liquidations: dict | None = None
    defi_tvl: dict | None = None
    news: list[dict] | None = None
    internal: dict | None = None

class Briefing(BaseModel):
    id: int | None = None
    briefing_type: str          # "morning" or "evening" or "manual"
    raw_data: dict              # all collected data
    synthesis: str              # formatted briefing text
    model_used: str
    tokens_used: int | None = None
    created_at: datetime
```

---

## 7. Database Schema

```sql
CREATE TABLE IF NOT EXISTS briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    briefing_type TEXT NOT NULL,
    raw_data TEXT NOT NULL,
    synthesis TEXT NOT NULL,
    model_used TEXT,
    tokens_used INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_briefings_created ON briefings(created_at);
```

---

## 8. Main Loop Integration

In `scout/narrative/agent.py` (or `scout/main.py`), add a time-gated briefing trigger:

```python
# Briefing schedule (default: 6am + 6pm UTC)
briefing_hours = [int(h) for h in settings.BRIEFING_HOURS_UTC.split(",")]

if settings.BRIEFING_ENABLED and now.hour in briefing_hours:
    if (now - last_briefing_at).total_seconds() > 39600:  # >11h gap
        try:
            from scout.briefing.collector import collect_briefing_data
            from scout.briefing.synthesizer import synthesize_briefing

            raw = await collect_briefing_data(session, db, settings)
            synthesis = await synthesize_briefing(raw, settings.ANTHROPIC_API_KEY, settings.BRIEFING_MODEL)

            # Store
            await db._conn.execute(
                "INSERT INTO briefings (briefing_type, raw_data, synthesis, model_used, created_at) VALUES (?, ?, ?, ?, ?)",
                (briefing_type, json.dumps(raw), synthesis, settings.BRIEFING_MODEL, now.isoformat()),
            )
            await db._conn.commit()

            # Telegram
            if settings.BRIEFING_TELEGRAM_ENABLED:
                # Split if > 4096 chars (Telegram limit)
                for chunk in _split_message(synthesis, 4096):
                    await send_telegram_message(chunk, session, settings)

            last_briefing_at = now
            logger.info("briefing_delivered", type=briefing_type)
        except Exception:
            logger.exception("briefing_error")
```

### Manual Trigger

Dashboard API endpoint for on-demand briefing:
```python
@app.post("/api/briefing/generate")
async def generate_briefing():
    """Manually trigger a briefing (doesn't wait for schedule)."""
```

---

## 9. Configuration

```python
# Market Briefing Agent
BRIEFING_ENABLED: bool = True
BRIEFING_HOURS_UTC: str = "6,18"             # comma-separated hours (6am + 6pm)
BRIEFING_MODEL: str = "claude-sonnet-4-6"
BRIEFING_TELEGRAM_ENABLED: bool = True
```

---

## 10. Dashboard — Briefing Tab

New tab positioned: Signals | Trading | Pipeline | **Briefing** | Health

### Layout

**Section 1: Latest Briefing**
- Full formatted text with styled section headers
- Timestamp + model info
- Each 📌 insight highlighted with a colored background

**Section 2: Previous Briefings**
- Scrollable list of past briefings
- Click to expand — shows full text
- Date + time + type (morning/evening/manual)

**Section 3: Controls**
- "Generate Now" button — triggers manual briefing via POST /api/briefing/generate
- Next scheduled briefing countdown
- Config display (schedule, model)

### API Endpoints

```
GET  /api/briefing/latest          — most recent briefing
GET  /api/briefing/history?limit=10 — past briefings
POST /api/briefing/generate        — trigger manual briefing
GET  /api/briefing/schedule        — next scheduled time
```

---

## 11. Telegram Delivery

The briefing can exceed Telegram's 4096 character limit. Split into multiple messages:

```python
def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split long briefing into Telegram-safe chunks, breaking at newlines."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Find last newline before limit
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
```

---

## 12. Error Handling

- **Any external API fails:** briefing still generates with available data. Synthesizer prompt includes: "If a data section shows null, note it as 'data unavailable' and work with what you have."
- **Claude API fails:** store raw data, retry synthesis next cycle. Send raw data summary to Telegram as fallback.
- **Telegram fails:** briefing still stored in DB and viewable on dashboard.
- **All external APIs fail:** briefing runs with internal data only (categories, catches, paper trades). Still valuable.

---

## 13. Testing Strategy

| Test File | Coverage |
|-----------|----------|
| `tests/test_briefing_collector.py` | Each fetch function with mocked HTTP, error handling, timeout |
| `tests/test_briefing_synthesizer.py` | Prompt formatting, Claude mock, response parsing |
| `tests/test_briefing_integration.py` | Full collect → synthesize → store → Telegram flow |

---

## 14. Out of Scope (v1)

- Historical trend analysis ("Fear & Greed was 45 last week, now 72")
- Automated trade recommendations based on briefing
- Multi-language briefings
- Custom briefing templates per user
- Voice/audio briefing delivery
