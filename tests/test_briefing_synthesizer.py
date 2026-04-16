"""Tests for briefing synthesizer — prompt formatting, message splitting."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scout.briefing.synthesizer import (
    format_user_prompt,
    split_message,
    synthesize_briefing,
)


class TestFormatUserPrompt:
    def test_includes_raw_data(self):
        raw = {
            "timestamp": "2026-04-16T06:00:00+00:00",
            "fear_greed": {"value": 72, "classification": "Greed"},
        }
        prompt = format_user_prompt(raw)
        assert "GECKO-ALPHA MARKET BRIEFING" in prompt
        assert '"value": 72' in prompt
        assert "2026-04-16" in prompt

    def test_handles_missing_timestamp(self):
        raw = {"fear_greed": None}
        prompt = format_user_prompt(raw)
        assert "GECKO-ALPHA MARKET BRIEFING" in prompt

    def test_all_section_headers_present(self):
        raw = {"timestamp": "2026-04-16T06:00:00+00:00"}
        prompt = format_user_prompt(raw)
        for header in [
            "MACRO PULSE",
            "BTC & ETH",
            "SECTOR ROTATION",
            "ON-CHAIN SIGNALS",
            "NEWS & CATALYSTS",
            "OUR EARLY CATCHES",
            "PAPER TRADING SNAPSHOT",
            "BOTTOM LINE",
        ]:
            assert header in prompt


class TestSplitMessage:
    def test_short_message(self):
        assert split_message("hello", 4096) == ["hello"]

    def test_exact_length(self):
        text = "a" * 4096
        assert split_message(text, 4096) == [text]

    def test_split_at_newline(self):
        text = "a" * 100 + "\n" + "b" * 100
        chunks = split_message(text, 150)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 100
        assert chunks[1] == "b" * 100

    def test_no_newline_splits_at_max(self):
        text = "a" * 200
        chunks = split_message(text, 100)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 100
        assert chunks[1] == "a" * 100

    def test_multiple_chunks(self):
        lines = [f"line {i}" for i in range(100)]
        text = "\n".join(lines)
        chunks = split_message(text, 200)
        assert len(chunks) > 1
        # All text should be preserved
        reassembled = "\n".join(chunks)
        assert reassembled == text


class TestSynthesizeBriefing:
    async def test_calls_anthropic(self):
        raw = {"timestamp": "2026-04-16T06:00:00+00:00", "fear_greed": {"value": 72}}

        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="Market briefing text here")]
        mock_message.usage = MagicMock(input_tokens=500, output_tokens=400)

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            result = await synthesize_briefing(raw, api_key="test-key", model="claude-sonnet-4-6")

        assert result == "Market briefing text here"
        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-sonnet-4-6"
        assert call_kwargs["max_tokens"] == 2000
        assert call_kwargs["temperature"] == 0.3
