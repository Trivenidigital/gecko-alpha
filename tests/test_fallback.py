"""Tests for Anthropic fallback narrative scorer."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

from scout.mirofish.fallback import score_narrative_fallback, FallbackScoringError
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


def _mock_anthropic_response(content: str):
    """Create a mock anthropic message response."""
    msg = MagicMock()
    block = MagicMock()
    block.text = content
    msg.content = [block]
    return msg


@pytest.mark.asyncio
async def test_fallback_parses_json_response():
    response_json = json.dumps(
        {
            "narrative_score": 65,
            "virality_class": "Medium",
            "summary": "Moderate viral potential.",
        }
    )

    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(response_json)

    result = await score_narrative_fallback(
        SAMPLE_SEED, "test-api-key", client=mock_client
    )

    assert isinstance(result, MiroFishResult)
    assert result.narrative_score == 65
    assert result.virality_class == "Medium"
    assert result.summary == "Moderate viral potential."


@pytest.mark.asyncio
async def test_fallback_success_does_not_log_raw_response():
    response_json = json.dumps(
        {
            "narrative_score": 65,
            "virality_class": "Medium",
            "summary": "Moderate viral potential.",
        }
    )

    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(response_json)

    with structlog.testing.capture_logs() as logs:
        result = await score_narrative_fallback(
            SAMPLE_SEED, "test-api-key", client=mock_client
        )

    assert result.narrative_score == 65
    assert all(entry.get("event") != "fallback_raw_response" for entry in logs)
    assert all(
        response_json not in str(value) for entry in logs for value in entry.values()
    )


@pytest.mark.asyncio
async def test_fallback_extracts_json_from_markdown():
    """Claude sometimes wraps JSON in ```json code blocks."""
    content = '```json\n{"narrative_score": 80, "virality_class": "High", "summary": "Very viral."}\n```'

    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(content)

    result = await score_narrative_fallback(
        SAMPLE_SEED, "test-api-key", client=mock_client
    )

    assert result.narrative_score == 80
    assert result.virality_class == "High"


@pytest.mark.asyncio
async def test_fallback_uses_correct_model():
    response_json = json.dumps(
        {
            "narrative_score": 50,
            "virality_class": "Low",
            "summary": "Weak narrative.",
        }
    )

    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(response_json)

    await score_narrative_fallback(SAMPLE_SEED, "test-api-key", client=mock_client)

    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-haiku-4-5"
    assert call_kwargs["max_tokens"] == 300


@pytest.mark.asyncio
async def test_fallback_raises_on_invalid_json():
    """Invalid JSON from LLM raises FallbackScoringError."""
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(
        "not json at all"
    )

    with pytest.raises(FallbackScoringError):
        await score_narrative_fallback(SAMPLE_SEED, "test-api-key", client=mock_client)


@pytest.mark.asyncio
async def test_fallback_error_keeps_truncated_raw_text():
    invalid_text = ("A" * 200) + "TAIL_SENTINEL_AFTER_200"
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(invalid_text)

    with pytest.raises(FallbackScoringError) as excinfo:
        await score_narrative_fallback(SAMPLE_SEED, "test-api-key", client=mock_client)

    message = str(excinfo.value)
    assert "Raw text:" in message
    assert "A" * 200 in message
    assert "TAIL_SENTINEL_AFTER_200" not in message


@pytest.mark.asyncio
async def test_fallback_raises_on_missing_keys():
    """JSON missing required keys raises FallbackScoringError."""
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(
        '{"foo": "bar"}'
    )

    with pytest.raises(FallbackScoringError):
        await score_narrative_fallback(SAMPLE_SEED, "test-api-key", client=mock_client)
