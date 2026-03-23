"""Anthropic fallback for narrative scoring when MiroFish is unavailable."""

import json
import structlog
import re

import anthropic

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


class FallbackScoringError(Exception):
    """Raised when the fallback LLM returns unparseable or invalid output."""


async def score_narrative_fallback(
    seed: dict,
    api_key: str,
    client: anthropic.AsyncAnthropic | None = None,
) -> MiroFishResult:
    """Score a token's narrative using Claude haiku as a fallback.

    Uses claude-haiku-4-5 with max_tokens=300. Returns the same MiroFishResult
    schema as the MiroFish client for compatibility with gate.py.
    """
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=api_key)

    message = await client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": seed["prompt"]}],
    )

    text = message.content[0].text
    logger.debug("fallback_raw_response", text=text[:300])

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
    # Try to find JSON in a code block first
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1).strip())
    # Otherwise try to parse the whole text
    return json.loads(text.strip())
