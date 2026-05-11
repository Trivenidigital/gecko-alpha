"""Tests for narrative-scoring fallback (Anthropic + OpenRouter via BL-NEW-LLM-ROUTER)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.mirofish.fallback import FallbackScoringError, score_narrative_fallback
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

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "test-api-key",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


def _mock_anthropic_response(content: str):
    """Create a mock anthropic message response."""
    msg = MagicMock()
    block = MagicMock()
    block.text = content
    msg.content = [block]
    return msg


# ============================================================
# Anthropic path (default provider — preserves pre-refactor behavior)
# ============================================================


@pytest.mark.asyncio
async def test_anthropic_parses_json_response():
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
        SAMPLE_SEED, _settings(), anthropic_client=mock_client
    )

    assert isinstance(result, MiroFishResult)
    assert result.narrative_score == 65
    assert result.virality_class == "Medium"
    assert result.summary == "Moderate viral potential."


@pytest.mark.asyncio
async def test_anthropic_extracts_json_from_markdown():
    """Claude sometimes wraps JSON in ```json code blocks."""
    content = '```json\n{"narrative_score": 80, "virality_class": "High", "summary": "Very viral."}\n```'
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(content)

    result = await score_narrative_fallback(
        SAMPLE_SEED, _settings(), anthropic_client=mock_client
    )

    assert result.narrative_score == 80
    assert result.virality_class == "High"


@pytest.mark.asyncio
async def test_anthropic_uses_correct_model():
    response_json = json.dumps(
        {
            "narrative_score": 50,
            "virality_class": "Low",
            "summary": "Weak narrative.",
        }
    )
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(response_json)

    await score_narrative_fallback(
        SAMPLE_SEED, _settings(), anthropic_client=mock_client
    )

    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-haiku-4-5"
    assert call_kwargs["max_tokens"] == 300


@pytest.mark.asyncio
async def test_anthropic_raises_on_invalid_json():
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response("not json")

    with pytest.raises(FallbackScoringError):
        await score_narrative_fallback(
            SAMPLE_SEED, _settings(), anthropic_client=mock_client
        )


@pytest.mark.asyncio
async def test_anthropic_raises_on_missing_keys():
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(
        '{"foo": "bar"}'
    )

    with pytest.raises(FallbackScoringError):
        await score_narrative_fallback(
            SAMPLE_SEED, _settings(), anthropic_client=mock_client
        )


@pytest.mark.asyncio
async def test_unknown_provider_falls_back_to_anthropic():
    """Fail-safe: unrecognized provider value uses Anthropic (known-good)."""
    response_json = json.dumps(
        {"narrative_score": 70, "virality_class": "High", "summary": "ok"}
    )
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = _mock_anthropic_response(response_json)

    result = await score_narrative_fallback(
        SAMPLE_SEED,
        _settings(MIROFISH_FALLBACK_PROVIDER="garbage"),
        anthropic_client=mock_client,
    )
    assert result.narrative_score == 70
    mock_client.messages.create.assert_called_once()


# ============================================================
# OpenRouter path (BL-NEW-LLM-ROUTER)
# ============================================================

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _openrouter_response(content: str) -> dict:
    """Mimic OpenRouter's OpenAI-compatible chat-completion response shape."""
    return {
        "id": "gen-test",
        "model": "moonshotai/kimi-k2-thinking",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


@pytest.mark.asyncio
async def test_openrouter_parses_json_response():
    s = _settings(
        MIROFISH_FALLBACK_PROVIDER="openrouter",
        OPENROUTER_API_KEY="sk-or-test",
    )
    content = json.dumps(
        {
            "narrative_score": 75,
            "virality_class": "High",
            "summary": "Strong narrative.",
        }
    )

    with aioresponses() as m:
        m.post(_OPENROUTER_URL, payload=_openrouter_response(content))
        result = await score_narrative_fallback(SAMPLE_SEED, s)

    assert result.narrative_score == 75
    assert result.virality_class == "High"
    assert result.summary == "Strong narrative."


@pytest.mark.asyncio
async def test_openrouter_extracts_json_from_markdown():
    s = _settings(
        MIROFISH_FALLBACK_PROVIDER="openrouter",
        OPENROUTER_API_KEY="sk-or-test",
    )
    content = '```json\n{"narrative_score": 55, "virality_class": "Medium", "summary": "Mid."}\n```'

    with aioresponses() as m:
        m.post(_OPENROUTER_URL, payload=_openrouter_response(content))
        result = await score_narrative_fallback(SAMPLE_SEED, s)

    assert result.narrative_score == 55
    assert result.virality_class == "Medium"


@pytest.mark.asyncio
async def test_openrouter_raises_on_missing_api_key():
    """Provider=openrouter without OPENROUTER_API_KEY must fail loudly, not
    silently fall through to a half-working state."""
    s = _settings(
        MIROFISH_FALLBACK_PROVIDER="openrouter",
        OPENROUTER_API_KEY="",  # explicitly empty
    )
    with pytest.raises(FallbackScoringError, match="OPENROUTER_API_KEY is unset"):
        await score_narrative_fallback(SAMPLE_SEED, s)


@pytest.mark.asyncio
async def test_openrouter_raises_on_http_error():
    s = _settings(
        MIROFISH_FALLBACK_PROVIDER="openrouter",
        OPENROUTER_API_KEY="sk-or-test",
    )
    with aioresponses() as m:
        m.post(_OPENROUTER_URL, status=402, body="insufficient credits")
        with pytest.raises(FallbackScoringError, match="OpenRouter HTTP 402"):
            await score_narrative_fallback(SAMPLE_SEED, s)


@pytest.mark.asyncio
async def test_openrouter_raises_on_malformed_response():
    """OpenRouter returns 200 with non-OpenAI-shape body → FallbackScoringError."""
    s = _settings(
        MIROFISH_FALLBACK_PROVIDER="openrouter",
        OPENROUTER_API_KEY="sk-or-test",
    )
    with aioresponses() as m:
        m.post(_OPENROUTER_URL, payload={"error": "model down"})
        with pytest.raises(FallbackScoringError, match="missing choices"):
            await score_narrative_fallback(SAMPLE_SEED, s)


@pytest.mark.asyncio
async def test_openrouter_uses_configured_model():
    """OPENROUTER_MODEL setting flows through to the payload."""
    s = _settings(
        MIROFISH_FALLBACK_PROVIDER="openrouter",
        OPENROUTER_API_KEY="sk-or-test",
        OPENROUTER_MODEL="moonshotai/kimi-k2",
    )
    content = json.dumps(
        {"narrative_score": 50, "virality_class": "Medium", "summary": "x"}
    )

    captured = {}

    def _capture(url, **kw):
        captured["payload"] = kw.get("json")
        captured["headers"] = kw.get("headers")

    with aioresponses() as m:
        m.post(
            _OPENROUTER_URL,
            payload=_openrouter_response(content),
            callback=_capture,
        )
        await score_narrative_fallback(SAMPLE_SEED, s)

    assert captured["payload"]["model"] == "moonshotai/kimi-k2"
    assert captured["payload"]["max_tokens"] == 300
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
