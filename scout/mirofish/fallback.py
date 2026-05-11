"""Narrative-scoring fallback when MiroFish is unavailable.

Despite the historic "fallback" name, this is the PRIMARY scoring path
in prod because MiroFish has never been deployed on the VPS (BL-034
DROPPED). All `mirofish_report` alert-body text comes from here.

BL-NEW-LLM-ROUTER (2026-05-11): supports two providers behind a Settings
flag so we can route this high-volume call site (60% of LLM spend) to
OpenRouter+Kimi while keeping signal-critical paths (predictor, counter
scorer) on Anthropic. Output feeds `alerter.py:48-49` text only — it does
NOT drive trade gates, so calibration drift risk is minimal.

Switch via .env: `MIROFISH_FALLBACK_PROVIDER=openrouter` + restart. Revert
by flipping back to `anthropic`. Both providers return the same
MiroFishResult schema.
"""

import json
import re

import aiohttp
import anthropic
import structlog

from scout.config import Settings
from scout.models import MiroFishResult

logger = structlog.get_logger()

SYSTEM_PROMPT = (
    "You are a crypto meme token narrative analyst evaluating viral spread potential. "
    "You are scoring MEME TOKENS — they are inherently speculative. Do not penalize "
    "tokens for being memes or lacking utility. Focus on: name memorability, "
    "cultural relevance, meme-ability, ticker appeal, and community formation potential.\n\n"
    "SCORING SCALE (calibrate your scores to this rubric):\n"
    "- 80-100 (Viral): Instantly memeable name, strong cultural hook, ticker is catchy, "
    "community would form organically. Examples: DOGE-tier narratives.\n"
    "- 60-79 (High): Good narrative hook, decent ticker, would get shared on CT. "
    "Most trending meme tokens should score here.\n"
    "- 40-59 (Medium): Generic but not bad. Has some angle but nothing standout.\n"
    "- 20-39 (Low): Weak name, no cultural hook, forgettable ticker.\n"
    "- 0-19 (None): Completely generic, no narrative angle whatsoever.\n\n"
    "IMPORTANT: The average meme token with a decent name should score 45-55. "
    "Do NOT default to low scores — most tokens that made it to trending have SOME narrative.\n\n"
    "If quantitative signals are provided, factor them in: strong buy pressure, "
    "high volume, and trending rank all suggest organic interest forming.\n\n"
    "Return ONLY a JSON object with these exact fields:\n"
    '{"narrative_score": <int 0-100>, "virality_class": "<Low|Medium|High|Viral>", '
    '"summary": "<2-3 sentence analysis>"}\n'
    "No other text. JSON only."
)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_TIMEOUT_SEC = 60


class FallbackScoringError(Exception):
    """Raised when the fallback LLM returns unparseable or invalid output."""


async def score_narrative_fallback(
    seed: dict,
    settings: Settings,
    *,
    anthropic_client: anthropic.AsyncAnthropic | None = None,
    aiohttp_session: aiohttp.ClientSession | None = None,
) -> MiroFishResult:
    """Score a token's narrative using the provider configured in Settings.

    Dispatches to Anthropic (claude-haiku-4-5) or OpenRouter (Kimi via
    `settings.OPENROUTER_MODEL`) based on `settings.MIROFISH_FALLBACK_PROVIDER`.

    Returns the same MiroFishResult schema regardless of provider.

    Test seam: pass `anthropic_client` or `aiohttp_session` to mock the
    underlying HTTP call without monkeypatching.
    """
    provider = (
        getattr(settings, "MIROFISH_FALLBACK_PROVIDER", "anthropic") or "anthropic"
    ).lower()
    if provider == "openrouter":
        return await _score_via_openrouter(seed, settings, aiohttp_session)
    # Default to anthropic for any unrecognized value (fail-safe to known-good).
    return await _score_via_anthropic(seed, settings, anthropic_client)


async def _score_via_anthropic(
    seed: dict,
    settings: Settings,
    client: anthropic.AsyncAnthropic | None,
) -> MiroFishResult:
    """Anthropic path — preserves pre-BL-NEW-LLM-ROUTER behavior."""
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    message = await client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": seed["prompt"]}],
    )
    text = message.content[0].text
    logger.debug("fallback_raw_response", provider="anthropic", text=text[:300])
    return _parse_result(text)


async def _score_via_openrouter(
    seed: dict,
    settings: Settings,
    session: aiohttp.ClientSession | None,
) -> MiroFishResult:
    """OpenRouter path — uses OpenAI-compatible chat-completions endpoint."""
    if not settings.OPENROUTER_API_KEY:
        raise FallbackScoringError(
            "MIROFISH_FALLBACK_PROVIDER=openrouter but OPENROUTER_API_KEY is unset"
        )

    payload = {
        "model": settings.OPENROUTER_MODEL,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": seed["prompt"]},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # OpenRouter recommends these headers for app identification.
        "HTTP-Referer": "https://github.com/Trivenidigital/gecko-alpha",
        "X-Title": "gecko-alpha",
    }
    timeout = aiohttp.ClientTimeout(total=_OPENROUTER_TIMEOUT_SEC)

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        async with session.post(
            _OPENROUTER_URL, json=payload, headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise FallbackScoringError(
                    f"OpenRouter HTTP {resp.status}: {body[:200]}"
                )
            data = await resp.json()
    finally:
        if owns_session:
            await session.close()

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise FallbackScoringError(
            f"OpenRouter response missing choices[0].message.content: {e}. "
            f"Body: {str(data)[:200]}"
        ) from e
    logger.debug(
        "fallback_raw_response",
        provider="openrouter",
        model=settings.OPENROUTER_MODEL,
        text=text[:300],
    )
    return _parse_result(text)


def _parse_result(text: str) -> MiroFishResult:
    """Parse the JSON narrative-scoring response (provider-agnostic)."""
    try:
        data = _extract_json(text)
        return MiroFishResult(
            narrative_score=int(data["narrative_score"]),
            virality_class=str(data["virality_class"]),
            summary=str(data["summary"]),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        raise FallbackScoringError(
            f"Failed to parse LLM response: {e}. Raw text: {text[:200]}"
        ) from e


def _extract_json(text: str) -> dict:
    """Extract JSON from text that may include markdown code blocks."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1).strip())
    return json.loads(text.strip())
