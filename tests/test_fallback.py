"""Tests for Claude API fallback narrative scorer."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scout.mirofish.fallback import score_narrative_fallback
from scout.models import MiroFishResult


SAMPLE_SEED = {
    "token_name": "TestCoin",
    "ticker": "TST",
    "chain": "solana",
    "market_cap": 50000,
    "age_hours": 60,
    "concept_description": "A meme token",
    "social_snippets": "None detected",
    "prompt": "Token: TestCoin (TST) on solana. Predict: will this narrative spread?",
}


def _mock_claude_response(content: str):
    """Create a mock anthropic message response."""
    msg = MagicMock()
    block = MagicMock()
    block.text = content
    msg.content = [block]
    return msg


@pytest.mark.asyncio
async def test_fallback_parses_json_response():
    response_json = json.dumps({
        "narrative_score": 65,
        "virality_class": "Medium",
        "summary": "Moderate viral potential.",
    })

    with patch("scout.mirofish.fallback.anthropic") as mock_anthropic:
        mock_client = AsyncMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(response_json)

        result = await score_narrative_fallback(SAMPLE_SEED, "test-api-key")

    assert isinstance(result, MiroFishResult)
    assert result.narrative_score == 65
    assert result.virality_class == "Medium"
    assert result.summary == "Moderate viral potential."


@pytest.mark.asyncio
async def test_fallback_extracts_json_from_markdown():
    """Claude sometimes wraps JSON in ```json code blocks."""
    content = '```json\n{"narrative_score": 80, "virality_class": "High", "summary": "Very viral."}\n```'

    with patch("scout.mirofish.fallback.anthropic") as mock_anthropic:
        mock_client = AsyncMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(content)

        result = await score_narrative_fallback(SAMPLE_SEED, "test-api-key")

    assert result.narrative_score == 80
    assert result.virality_class == "High"


@pytest.mark.asyncio
async def test_fallback_uses_correct_model():
    response_json = json.dumps({
        "narrative_score": 50,
        "virality_class": "Low",
        "summary": "Weak narrative.",
    })

    with patch("scout.mirofish.fallback.anthropic") as mock_anthropic:
        mock_client = AsyncMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_claude_response(response_json)

        await score_narrative_fallback(SAMPLE_SEED, "test-api-key")

    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-haiku-4-5"
    assert call_kwargs["max_tokens"] == 300
