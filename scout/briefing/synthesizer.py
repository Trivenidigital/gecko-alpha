"""Synthesize raw briefing data into analyst-grade market intelligence.

Uses a single Claude Sonnet call to transform structured data into
a formatted, actionable briefing.
"""

import json
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

SYSTEM_PROMPT = """You are a senior crypto market analyst preparing a structured briefing for a trader.
Your job: synthesize raw market data into actionable intelligence.

Rules:
- Start each section with a \U0001f4cc insight that connects the data to a trading thesis
- Be specific with numbers -- don't say "up significantly", say "+18.2%"
- Connect dots across sections -- if BTC dominance is falling AND alt categories are heating, say so
- Flag contradictions -- if sentiment is "Greed" but funding is negative, note the divergence
- Keep each section to 3-5 bullet points max
- End with 1-2 sentences: "Bottom line: [market stance]"
- Use the exact section headers and emoji provided
- If a data section shows null, note it as 'data unavailable' and work with what you have"""

_USER_PROMPT_TEMPLATE = """Generate a market briefing from the following data collected at {timestamp}.

=== RAW DATA ===
{raw_data_json}

=== REQUIRED SECTIONS ===

\U0001f50d GECKO-ALPHA MARKET BRIEFING -- {date_formatted}

\U0001f4ca MACRO PULSE
- Fear & Greed index, direction, what it signals
- Total market cap + 24h change
- BTC dominance + trend implication
- \U0001f4cc One key macro insight connecting these

\U0001f4c8 BTC & ETH
- BTC price, 24h change, key data point (ETF flows, exchange flows)
- ETH price, 24h change, key data point (staking, L2 activity)
- \U0001f4cc What BTC+ETH behavior signals for the broader market

\U0001f525 SECTOR ROTATION
- Top 3 heating categories with acceleration %
- Top 3 cooling categories
- \U0001f4cc Which narrative rotation to watch and why

\u26d3\ufe0f ON-CHAIN SIGNALS
- Funding rates (bullish/bearish/neutral interpretation)
- Liquidation data (who's getting liquidated, what it means)
- DeFi TVL trend
- \U0001f4cc On-chain conviction signal

\U0001f4f0 NEWS & CATALYSTS
- Top 3-5 most impactful headlines
- \U0001f4cc Which news is most likely to move markets in the next 12h

\U0001f3af OUR EARLY CATCHES
- Tokens we detected before trending (with lead time + peak gain)
- Active narrative predictions
- \U0001f4cc Our system's current edge

\U0001f4ca PAPER TRADING SNAPSHOT
- Open positions + unrealized PnL
- By signal type performance
- \U0001f4cc Which signal types are producing alpha

\U0001f4a1 BOTTOM LINE
- 1-2 sentence market stance
- Key levels or events to watch in next 12h

Return ONLY the formatted briefing text. No JSON, no code blocks."""


def format_user_prompt(raw_data: dict) -> str:
    """Build the user prompt from raw collected data."""
    ts = raw_data.get("timestamp", datetime.now(timezone.utc).isoformat())
    try:
        dt = datetime.fromisoformat(ts)
        date_fmt = dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        date_fmt = ts

    return _USER_PROMPT_TEMPLATE.format(
        timestamp=ts,
        raw_data_json=json.dumps(raw_data, indent=2, default=str),
        date_formatted=date_fmt,
    )


async def synthesize_briefing(
    raw_data: dict,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Call Claude to synthesize raw data into formatted briefing.

    Returns the formatted briefing text.
    Raises on API failure (caller should handle).
    """
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=90.0)
    message = await client.messages.create(
        model=model,
        max_tokens=2000,
        temperature=0.3,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": format_user_prompt(raw_data)}],
    )
    text = message.content[0].text
    logger.info(
        "briefing_synthesized",
        model=model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
    )
    return text


def split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split long briefing into Telegram-safe chunks, breaking at newlines."""
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
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
