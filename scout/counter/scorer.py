"""Counter-narrative scoring orchestrator — calls Claude to synthesize risk flags."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import structlog

from scout.counter.models import CounterScore, RedFlag
from scout.counter.prompts import (
    COUNTER_MEMECOIN_TEMPLATE,
    COUNTER_NARRATIVE_TEMPLATE,
    COUNTER_SYSTEM,
    format_flags_for_prompt,
)

log = structlog.get_logger()

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def _parse_counter_response(text: str) -> dict | None:
    """Extract JSON from a Claude response, handling markdown code blocks.

    Returns parsed dict on success, None on any parse failure.
    """
    # Try extracting from markdown ```json block first
    match = _JSON_BLOCK_RE.search(text)
    if match:
        candidate = match.group(1).strip()
    else:
        candidate = text.strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
        return None
    except (json.JSONDecodeError, TypeError):
        return None


async def score_counter_narrative(
    token_name: str,
    symbol: str,
    market_cap: float,
    price_change_24h: float,
    category_name: str,
    acceleration: float,
    narrative_fit_score: float,
    flags: list[RedFlag],
    data_completeness: str,
    api_key: str,
    model: str = "claude-haiku-4-5",
    client: object | None = None,
) -> CounterScore:
    """Score counter-narrative for a narrative-driven token via Claude.

    Always returns a CounterScore (never raises). On failure, risk_score is None
    and counter_argument is empty.
    """
    try:
        from anthropic import AsyncAnthropic

        if client is None:
            client = AsyncAnthropic(api_key=api_key)

        prompt = COUNTER_NARRATIVE_TEMPLATE.format(
            token_name=token_name,
            symbol=symbol,
            market_cap=market_cap,
            price_change_24h=price_change_24h,
            category_name=category_name,
            acceleration=acceleration,
            narrative_fit_score=narrative_fit_score,
            formatted_flags=format_flags_for_prompt(flags),
            data_completeness=data_completeness,
        )

        response = await client.messages.create(
            model=model,
            system=COUNTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )

        raw_text = response.content[0].text
        parsed = _parse_counter_response(raw_text)

        if parsed is None:
            log.warning(
                "counter_narrative_parse_failed",
                token=token_name,
                raw=raw_text[:200],
            )
            return CounterScore(data_completeness=data_completeness)

        return CounterScore(
            risk_score=parsed.get("risk_score"),
            red_flags=flags,
            counter_argument=parsed.get("counter_argument", ""),
            data_completeness=data_completeness,
            counter_scored_at=datetime.now(timezone.utc),
        )

    except Exception:
        log.exception("counter_narrative_error", token=token_name)
        return CounterScore(data_completeness=data_completeness)


async def score_counter_memecoin(
    token_name: str,
    symbol: str,
    chain: str,
    token_age_days: float,
    liquidity_usd: float,
    vol_liq_ratio: float,
    buy_pressure: float,
    holder_count: int,
    flags: list[RedFlag],
    data_completeness: str,
    api_key: str,
    model: str = "claude-haiku-4-5",
    client: object | None = None,
) -> CounterScore:
    """Score counter-narrative for a memecoin token via Claude.

    Always returns a CounterScore (never raises). On failure, risk_score is None
    and counter_argument is empty.
    """
    try:
        from anthropic import AsyncAnthropic

        if client is None:
            client = AsyncAnthropic(api_key=api_key)

        token_age_hours = token_age_days * 24

        prompt = COUNTER_MEMECOIN_TEMPLATE.format(
            token_name=token_name,
            symbol=symbol,
            chain=chain,
            token_age_hours=token_age_hours,
            liquidity=liquidity_usd,
            vol_liq_ratio=vol_liq_ratio,
            buy_pressure=buy_pressure,
            holder_count=holder_count,
            formatted_flags=format_flags_for_prompt(flags),
            data_completeness=data_completeness,
        )

        response = await client.messages.create(
            model=model,
            system=COUNTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )

        raw_text = response.content[0].text
        parsed = _parse_counter_response(raw_text)

        if parsed is None:
            log.warning(
                "counter_memecoin_parse_failed",
                token=token_name,
                raw=raw_text[:200],
            )
            return CounterScore(data_completeness=data_completeness)

        return CounterScore(
            risk_score=parsed.get("risk_score"),
            red_flags=flags,
            counter_argument=parsed.get("counter_argument", ""),
            data_completeness=data_completeness,
            counter_scored_at=datetime.now(timezone.utc),
        )

    except Exception:
        log.exception("counter_memecoin_error", token=token_name)
        return CounterScore(data_completeness=data_completeness)
