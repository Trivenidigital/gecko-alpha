"""Tests for alert delivery."""

import pytest
import aiohttp
from aioresponses import aioresponses

from scout.alerter import (
    _escape_md,
    send_alert,
    send_telegram_message,
    format_alert_message,
    format_daily_summary,
)


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


def test_format_alert_message_contains_required_fields(token_factory):
    token = token_factory(
        contract_address="0xabc123",
        chain="solana",
        token_name="MoonCoin",
        ticker="MOON",
        token_age_days=2,
        market_cap_usd=75000,
        liquidity_usd=15000,
        volume_24h_usd=120000,
        holder_count=350,
        holder_growth_1h=30,
        quant_score=80,
        narrative_score=75,
        conviction_score=78,
        virality_class="High",
        mirofish_report="Strong viral narrative.",
    )
    signals = ["vol_liq_ratio", "holder_growth", "market_cap_range"]
    msg = format_alert_message(token, signals)

    assert "RESEARCH ONLY" in msg
    assert "MoonCoin" in msg
    assert "MOON" in msg
    assert "solana" in msg
    assert "75,000" in msg or "75000" in msg
    assert "78" in msg  # conviction score
    assert "80" in msg  # quant score
    assert "75" in msg  # narrative score
    assert "High" in msg  # virality class
    assert r"vol\_liq\_ratio" in msg
    assert (
        "[chart](https://dexscreener.com" in msg
    ), "URL must be wrapped in [chart](url) so MarkdownV1 does not parse special chars inside the URL"
    assert "0xabc123" in msg


def test_format_alert_message_without_narrative(token_factory):
    token = token_factory(
        contract_address="0xabc123",
        chain="solana",
        token_name="MoonCoin",
        ticker="MOON",
        token_age_days=2,
        market_cap_usd=75000,
        liquidity_usd=15000,
        volume_24h_usd=120000,
        holder_count=350,
        holder_growth_1h=30,
        quant_score=80,
        narrative_score=None,
        virality_class=None,
        mirofish_report=None,
        conviction_score=80,
    )
    signals = ["vol_liq_ratio"]
    msg = format_alert_message(token, signals)

    assert "RESEARCH ONLY" in msg
    assert "MoonCoin" in msg


def test_format_alert_message_omits_conviction_line_when_score_none(token_factory):
    """SIG-01 / ALR-05: the conviction gate is retired; a token with no
    conviction score must NOT lead with the "Conviction Score: N/A"
    shrug-headline. The whole conviction breakdown is dropped instead."""
    token = token_factory(
        contract_address="0xabc123",
        chain="solana",
        token_name="MoonCoin",
        ticker="MOON",
        market_cap_usd=75000,
        quant_score=54,
        narrative_score=None,
        conviction_score=None,
        virality_class=None,
        mirofish_report=None,
    )
    msg = format_alert_message(token, ["vol_liq_ratio"])

    assert "Conviction Score" not in msg
    assert "N/A" not in msg
    # Body still renders identity + signals — only the null-score line is gone.
    assert "MoonCoin" in msg
    assert r"vol\_liq\_ratio" in msg


def test_format_alert_message_includes_conviction_line_when_score_present(
    token_factory,
):
    """A real conviction score is still rendered (regression pin for the
    re-enabled path)."""
    token = token_factory(
        contract_address="0xabc123",
        chain="solana",
        token_name="MoonCoin",
        ticker="MOON",
        market_cap_usd=75000,
        quant_score=80,
        narrative_score=75,
        conviction_score=78,
    )
    msg = format_alert_message(token, ["vol_liq_ratio"])

    assert "Conviction Score: 78.0" in msg
    assert "Quant: 80" in msg
    assert "Narrative: 75" in msg


async def test_send_alert_telegram(mock_aiohttp, token_factory, settings_factory):
    telegram_url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(telegram_url, payload={"ok": True})

    token = token_factory(
        contract_address="0xabc123",
        chain="solana",
        token_name="MoonCoin",
        ticker="MOON",
        quant_score=80,
        narrative_score=75,
        conviction_score=78,
        virality_class="High",
        mirofish_report="Strong viral narrative.",
    )
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    signals = ["vol_liq_ratio", "holder_growth"]

    async with aiohttp.ClientSession() as session:
        await send_alert(token, signals, session, settings)


async def test_send_telegram_message_logs_delivered_on_200(
    mock_aiohttp, settings_factory
):
    """§12b systemic observability: a confirmed 200 emits
    telegram_message_delivered with the callsite source. The alerter previously
    logged ONLY on failure, so a successful send was indistinguishable from a
    send that was never called."""
    from structlog.testing import capture_logs

    telegram_url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(telegram_url, payload={"ok": True})
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    async with aiohttp.ClientSession() as session:
        with capture_logs() as logs:
            await send_telegram_message(
                "hello", session, settings, parse_mode=None, source="unit_test"
            )
    delivered = [e for e in logs if e["event"] == "telegram_message_delivered"]
    assert len(delivered) == 1
    assert delivered[0]["source"] == "unit_test"


def test_alert_message_includes_momentum_flag(token_factory):
    """AC-08: Momentum flag appears in alert message when signal fired."""
    token = token_factory(
        contract_address="0xabc123",
        chain="solana",
        token_name="MoonCoin",
        ticker="MOON",
        quant_score=80,
        narrative_score=75,
        conviction_score=78,
        virality_class="High",
        mirofish_report="Strong viral narrative.",
    )
    signals = ["vol_liq_ratio", "momentum_ratio", "vol_acceleration"]
    msg = format_alert_message(token, signals)
    assert "CoinGecko Signals" in msg
    assert "1h gain accelerating" in msg.lower() or "momentum" in msg.lower()


def test_alert_message_includes_vol_spike_flag(token_factory):
    """Vol spike flag appears in alert message when signal fired."""
    token = token_factory(
        contract_address="0xabc123",
        chain="solana",
        token_name="MoonCoin",
        ticker="MOON",
        quant_score=80,
        narrative_score=75,
        conviction_score=78,
        virality_class="High",
        mirofish_report="Strong viral narrative.",
    )
    signals = ["vol_acceleration"]
    msg = format_alert_message(token, signals)
    assert "CoinGecko Signals" in msg
    assert "volume spike" in msg.lower() or "vol >>" in msg.lower()


def test_escape_md_backslash_escaped_first():
    """A literal backslash in input must be escaped once, not re-escaped
    after an underscore is prefixed with its own backslash. The ordering of
    _MD_ESCAPE_CHARS puts backslash first so we never double-escape.
    """
    # A literal backslash becomes '\\' (escaped backslash), not '\\\\' (double).
    assert _escape_md("a\\b") == "a\\\\b"
    # Literal backslash AND underscore: each gets ONE preceding backslash.
    assert _escape_md("a\\_b") == "a\\\\\\_b"


def test_escape_md_protects_underscore_star_bracket_tick():
    """All Markdown-v1 metachars are escaped."""
    assert _escape_md("AS_ROID") == r"AS\_ROID"
    assert _escape_md("*bold*") == r"\*bold\*"
    assert _escape_md("[link]") == r"\[link\]"
    assert _escape_md("`code`") == r"\`code\`"


def test_escape_md_handles_none_and_non_string():
    """None returns empty string; ints/floats are coerced via str()."""
    assert _escape_md(None) == ""
    assert _escape_md(42) == "42"


def test_daily_summary_is_plain_text_for_parse_mode_none():
    """Daily summary is sent with parse_mode=None, so do not emit Markdown."""
    msg = format_daily_summary(
        {
            "alerts_today": 2,
            "outcomes_total": 1,
            "outcomes_wins": 1,
            "win_rate_pct": 100.0,
            "top_signal_combo": '["gainers_early"]',
            "top_tokens": [
                {
                    "token_name": "AS_ROID",
                    "ticker": "AS_R",
                    "conviction_score": 91.5,
                    "quant_score": 90,
                    "narrative_score": 93,
                }
            ],
        }
    )
    assert "*" not in msg
    assert "AS_ROID" in msg


async def test_send_alert_telegram_and_discord(
    mock_aiohttp, token_factory, settings_factory
):
    telegram_url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    discord_url = "https://discord.com/api/webhooks/test"

    mock_aiohttp.post(telegram_url, payload={"ok": True})
    mock_aiohttp.post(discord_url, payload={}, status=204)

    token = token_factory(
        contract_address="0xabc123",
        chain="solana",
        token_name="MoonCoin",
        ticker="MOON",
        quant_score=80,
        narrative_score=75,
        conviction_score=78,
        virality_class="High",
        mirofish_report="Strong viral narrative.",
    )
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/test",
    )
    signals = ["vol_liq_ratio"]

    async with aiohttp.ClientSession() as session:
        await send_alert(token, signals, session, settings)


# ---------------------------------------------------------------------------
# B1 delivery-certainty taxonomy at the alerter seam: an explicit non-200 is a
# CONFIRMED rejection (TelegramRejected); a transport failure after the request
# begins is UNPROVABLE (TelegramTransportUnknown). A generic transport exception
# must never be conflated with a confirmed rejection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_telegram_explicit_non200_raises_rejected(
    mock_aiohttp, settings_factory
):
    from scout.exceptions import TelegramRejected

    telegram_url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(
        telegram_url, status=400, payload={"ok": False, "description": "bad"}
    )
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramRejected):
            await send_telegram_message(
                "hi",
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="unit_test",
            )


@pytest.mark.asyncio
async def test_send_telegram_transport_error_raises_transport_unknown(
    mock_aiohttp, settings_factory
):
    from scout.exceptions import TelegramTransportUnknown

    telegram_url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    # aiohttp raises a client connection error AFTER the request begins → unprovable.
    mock_aiohttp.post(
        telegram_url, exception=aiohttp.ClientConnectionError("connection reset")
    )
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramTransportUnknown):
            await send_telegram_message(
                "hi",
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="unit_test",
            )


@pytest.mark.asyncio
async def test_send_telegram_timeout_raises_transport_unknown(
    mock_aiohttp, settings_factory
):
    import asyncio as _asyncio

    from scout.exceptions import TelegramTransportUnknown

    telegram_url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(telegram_url, exception=_asyncio.TimeoutError())
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramTransportUnknown):
            await send_telegram_message(
                "hi",
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="unit_test",
            )


@pytest.mark.asyncio
async def test_send_telegram_5xx_raises_transport_unknown(
    mock_aiohttp, settings_factory
):
    """B1 certainty split: a 5xx server error is UNPROVABLE (the request reached
    Telegram but the server erred) → TelegramTransportUnknown, NOT a confirmed
    rejection. (A 4xx maps to TelegramRejected — see the non200 test above.)"""
    from scout.exceptions import TelegramRejected, TelegramTransportUnknown

    telegram_url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(telegram_url, status=503, body="service unavailable")
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramTransportUnknown):
            await send_telegram_message(
                "hi",
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="unit_test",
            )
        assert not issubclass(TelegramTransportUnknown, TelegramRejected)


@pytest.mark.asyncio
async def test_send_telegram_200_ok_false_is_rejected(mock_aiohttp, settings_factory):
    """B1: HTTP 200 with an explicit ok:false body is a definitive non-acceptance
    → TelegramRejected. An HTTP 200 by itself NEVER proves delivery."""
    from scout.exceptions import TelegramRejected

    url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(url, status=200, payload={"ok": False, "description": "blocked"})
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramRejected):
            await send_telegram_message(
                "hi",
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="unit_test",
            )


@pytest.mark.asyncio
async def test_send_telegram_200_unparseable_is_transport_unknown(
    mock_aiohttp, settings_factory
):
    """B1: HTTP 200 whose body does NOT prove acceptance (unparseable / no ok:true)
    → TelegramTransportUnknown (delivery unprovable) — NEVER sent."""
    from scout.exceptions import TelegramTransportUnknown

    url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(url, status=200, body="<html>not json</html>")
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramTransportUnknown):
            await send_telegram_message(
                "hi",
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="unit_test",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 409])
async def test_send_telegram_ambiguous_4xx_is_transport_unknown(
    mock_aiohttp, settings_factory, status
):
    """B1: 408 (request timeout — server may have processed) and 409 (conflict) do
    NOT prove non-acceptance → TelegramTransportUnknown, not a confirmed rejection."""
    from scout.exceptions import TelegramTransportUnknown

    url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(url, status=status, body="ambiguous")
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramTransportUnknown):
            await send_telegram_message(
                "hi",
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="unit_test",
            )


@pytest.mark.asyncio
async def test_send_telegram_429_rejects_and_preserves_retry_after(
    mock_aiohttp, settings_factory
):
    """B1: a 429 is a definitive rate-limit rejection → TelegramRejected (never
    counted as delivered), and its retry_after metadata is preserved (logged) as it
    flows today."""
    from structlog.testing import capture_logs

    from scout.exceptions import TelegramRejected

    url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    # over-budget retry_after → no in-band retry; falls through to the rejection.
    mock_aiohttp.post(
        url, status=429, payload={"ok": False, "parameters": {"retry_after": 5}}
    )
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
        TG_PACING_ENABLED=True,
        TG_PACING_MAX_WAIT_SECONDS=1,
    )
    async with aiohttp.ClientSession() as session:
        with capture_logs() as logs:
            with pytest.raises(TelegramRejected):
                await send_telegram_message(
                    "hi",
                    session,
                    settings,
                    parse_mode=None,
                    raise_on_failure=True,
                    source="unit_test",
                )
    assert any("retry_after" in e for e in logs)  # retry_after preserved in logs


# ---------------------------------------------------------------------------
# B1 proof-1: the response body is consumed + classified EXACTLY ONCE through the
# real provider path (_post_telegram_once -> typed outcome). No second read, no
# downstream re-interpretation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_response_consumed_and_classified_exactly_once(
    mock_aiohttp, settings_factory, monkeypatch
):
    import scout.alerter as alerter_mod

    calls = {"post": 0, "classify": 0}
    real_post = alerter_mod._post_telegram_once
    real_classify = alerter_mod._telegram_accepted

    async def _counting_post(*a, **k):
        calls["post"] += 1
        return await real_post(*a, **k)

    def _counting_classify(b):
        calls["classify"] += 1
        return real_classify(b)

    monkeypatch.setattr(alerter_mod, "_post_telegram_once", _counting_post)
    monkeypatch.setattr(alerter_mod, "_telegram_accepted", _counting_classify)

    url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(url, status=200, payload={"ok": True})
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    async with aiohttp.ClientSession() as session:
        await send_telegram_message(
            "hi",
            session,
            settings,
            parse_mode=None,
            raise_on_failure=True,
            source="t",
        )
    assert calls["post"] == 1  # single HTTP fetch + single body read
    assert calls["classify"] == 1  # classified exactly once, no re-interpretation


# ---------------------------------------------------------------------------
# B1 proof-3: retry_after is CONTAINED — it drives only logs/metrics + the
# existing bounded in-call retry (ONE retry, inside the deadline). It NEVER
# schedules a new dispatch. Over budget -> zero in-band retries (1 POST); in
# budget -> at most one bounded retry (2 POSTs), then classification stops.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_429_over_budget_no_inband_retry(
    mock_aiohttp, settings_factory, monkeypatch
):
    import scout.alerter as alerter_mod

    posts = {"n": 0}
    real_post = alerter_mod._post_telegram_once

    async def _counting_post(*a, **k):
        posts["n"] += 1
        return await real_post(*a, **k)

    monkeypatch.setattr(alerter_mod, "_post_telegram_once", _counting_post)
    from scout.exceptions import TelegramRejected

    url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(
        url, status=429, payload={"ok": False, "parameters": {"retry_after": 5}}
    )
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
        TG_PACING_ENABLED=True,
        TG_PACING_MAX_WAIT_SECONDS=1,  # retry_after=5 is OVER budget
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramRejected):
            await send_telegram_message(
                "hi",
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="t",
            )
    assert posts["n"] == 1  # retry_after over budget -> NO in-band resend


@pytest.mark.asyncio
async def test_b1_429_in_budget_bounded_single_retry(
    mock_aiohttp, settings_factory, monkeypatch
):
    import scout.alerter as alerter_mod

    posts = {"n": 0}
    real_post = alerter_mod._post_telegram_once

    async def _counting_post(*a, **k):
        posts["n"] += 1
        return await real_post(*a, **k)

    monkeypatch.setattr(alerter_mod, "_post_telegram_once", _counting_post)

    url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    # first a 429 (in budget), then the ONE bounded retry gets a clean 200 ok:true.
    mock_aiohttp.post(
        url, status=429, payload={"ok": False, "parameters": {"retry_after": 0}}
    )
    mock_aiohttp.post(url, status=200, payload={"ok": True})
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
        TG_PACING_ENABLED=True,
        TG_PACING_MAX_WAIT_SECONDS=5,  # retry_after=0 is IN budget
    )
    async with aiohttp.ClientSession() as session:
        await send_telegram_message(
            "hi",
            session,
            settings,
            parse_mode=None,
            raise_on_failure=True,
            source="t",
        )
    assert posts["n"] == 2  # exactly ONE bounded in-call retry; never unbounded
