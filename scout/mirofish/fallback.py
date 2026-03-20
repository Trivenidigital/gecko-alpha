"""Claude API fallback for narrative scoring when MiroFish is unavailable."""

import json
import structlog
import re

import anthropic

from scout.models import MiroFishResult

logger = structlog.get_logger()

SYSTEM_PROMPT = (
    "You are a crypto narrative analyst. Score the viral potential of a token's "
    "narrative. Return ONLY a JSON object with these exact fields:\n"
    '{"narrative_score": <int 0-100>, "virality_class": "<Low|Medium|High|Viral>", '
    '"summary": "<2-3 sentence analysis>"}\n'
    "No other text. JSON only."
)


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
    data = _extract_json(text)

    return MiroFishResult(
        narrative_score=int(data["narrative_score"]),
        virality_class=str(data["virality_class"]),
        summary=str(data["summary"]),
    )


def _extract_json(text: str) -> dict:
    """Extract JSON from text that may include markdown code blocks."""
    # Try to find JSON in a code block first
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1).strip())
    # Otherwise try to parse the whole text
    return json.loads(text.strip())
