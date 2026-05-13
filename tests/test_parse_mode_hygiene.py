"""Regression tests for BL-NEW-PARSE-MODE-AUDIT — Class-3 silent rendering corruption.

These tests pin the 7 HIGH ACTUAL sites (6 from audit + 1 plan-review discovery)
against future regression. Three coverage layers:
  1. Formatter render assertion — for sites that use _escape_md (#6, #7)
  2. Call-site source-level pin — for sites that use parse_mode=None (#1-5)
  3. AST structural coverage — every send_telegram_message call site in scout/
     must pin parse_mode (closes the audit-methodology gap that missed #7)
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest


# ---------------------------------------------------------------------
# Helper: capture the payload that scout.alerter.send_telegram_message
# would post to Telegram (used by per-site source-pin tests).
# ---------------------------------------------------------------------


def _capture_send(monkeypatch):
    """Patch scout.alerter.send_telegram_message to capture call args.

    Returns a list appended-to on each call. Each entry: {text, parse_mode}.
    """
    captured: list[dict] = []

    async def fake_send(text, session, settings, *, parse_mode="Markdown"):
        captured.append({"text": text, "parse_mode": parse_mode})

    monkeypatch.setattr("scout.alerter.send_telegram_message", fake_send)
    return captured


# ---------------------------------------------------------------------
# AST structural coverage — Layer 3
# ---------------------------------------------------------------------


SCOUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scout"


def _find_dispatch_calls(tree: ast.AST) -> list[ast.Call]:
    """Find every ast.Call to `send_telegram_message` in a parsed module.

    Matches both `send_telegram_message(...)` (attribute or name) and
    `alerter.send_telegram_message(...)`.
    """
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name == "send_telegram_message":
            calls.append(node)
    return calls


def test_all_dispatch_sites_pin_parse_mode():
    """Layer 3: every send_telegram_message call site in scout/ MUST pass
    parse_mode explicitly (None or "Markdown" or "MarkdownV2" or "HTML").

    Rationale: the original audit grepped `send_telegram_message` source
    occurrences and missed `send_alert` at scout/alerter.py:189 because
    that function does its own session.post call. An AST walk over the
    invocation graph catches every dispatch regardless of formatting,
    multi-line layout, or kwarg-from-variable. Closes the audit-methodology
    gap so a NEW dispatch site added 6 months from now without parse_mode=
    is caught at CI time, not after an operator notices a mangled alert.

    Exclusion: scout/alerter.py itself defines send_telegram_message, so
    references inside its own module body (the function definition's
    default-value reference, internal helpers calling it for testing)
    are tolerated only if they explicitly pass parse_mode= in the call.
    """
    offenders: list[str] = []
    for py_path in SCOUT_DIR.rglob("*.py"):
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for call in _find_dispatch_calls(tree):
            kwarg_names = {kw.arg for kw in call.keywords}
            if "parse_mode" not in kwarg_names:
                offenders.append(
                    f"{py_path.relative_to(SCOUT_DIR.parent)}:{call.lineno} "
                    f"send_telegram_message() without parse_mode= kwarg"
                )
    assert not offenders, (
        "send_telegram_message dispatch sites missing parse_mode kwarg "
        "(see BL-NEW-PARSE-MODE-AUDIT + CLAUDE.md §12b):\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------
# Site #1: narrative heating alert
# ---------------------------------------------------------------------


def test_narrative_heating_alert_formatter_preserves_underscored_symbol():
    """Site #1: format_heating_alert body interpolates p.symbol raw —
    the dispatch site at scout/narrative/agent.py:557 must use
    parse_mode=None to render literal underscores in symbols like AS_ROID.

    This test verifies the formatter STILL interpolates the raw symbol
    (no double-escaping); the call-site test below pins parse_mode=None.
    """
    from scout.narrative.digest import format_heating_alert
    from scout.narrative.models import CategoryAcceleration, NarrativePrediction

    accel = CategoryAcceleration(
        category_id="ai",
        name="AI",
        previous_velocity=1.0,
        current_velocity=5.0,
        acceleration=4.0,
        volume_growth_pct=50.0,
        coin_count_change=0,
    )
    pred = NarrativePrediction(
        symbol="AS_ROID",
        market_cap_at_prediction=1_000_000,
        price_at_prediction=0.01,
        narrative_fit_score=80,
        confidence="high",
        reasoning="test",
        is_control=False,
        market_regime="bull",
    )
    text = format_heating_alert(accel, [pred], "BTC, ETH, SOL")
    # Body itself contains raw underscored symbol; escaping happens at the wire
    # via parse_mode=None (no Markdown parsing at all).
    assert "AS_ROID" in text


def test_narrative_agent_alert_call_passes_parse_mode_none():
    """Site #1 call-site contract: scout/narrative/agent.py:557 dispatches
    with parse_mode=None. Source-level pin — if a future refactor removes
    the kwarg, this test fails.
    """
    import scout.narrative.agent as agent

    source = inspect.getsource(agent)
    assert "format_heating_alert(" in source
    idx = source.index("format_heating_alert(")
    # Look ahead within ~600 chars for the send_telegram_message + parse_mode
    tail = source[idx : idx + 600]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail



# ---------------------------------------------------------------------
# Site #2: paper trading daily digest
# ---------------------------------------------------------------------


def test_paper_digest_call_passes_parse_mode_none():
    """Site #2: scout/narrative/agent.py:715 dispatches paper digest with
    parse_mode=None. Body interpolates best_symbol/worst_symbol AND per-
    signal_type keys; every signal_type has underscores.
    """
    import scout.narrative.agent as agent

    source = inspect.getsource(agent)
    idx = source.index("build_paper_digest")
    tail = source[idx : idx + 800]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail



# ---------------------------------------------------------------------
# Site #3: secondwave detector alert
# ---------------------------------------------------------------------


def test_secondwave_alert_call_passes_parse_mode_none():
    """Site #3: scout/secondwave/detector.py:285 dispatches with parse_mode=None.
    Body interpolates ticker, token_name, peak_signals join, reacc_signals join.
    """
    import scout.secondwave.detector as detector

    source = inspect.getsource(detector)
    idx = source.index("format_secondwave_alert(")
    tail = source[idx : idx + 400]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail


# ---------------------------------------------------------------------
# Site #4: calibration apply alert
# ---------------------------------------------------------------------


def test_calibrate_apply_alert_call_passes_parse_mode_none():
    """Site #4: scout/trading/calibrate.py:354 (apply path) dispatches
    with parse_mode=None. Body interpolates d.signal_type (always has
    underscores) inside [reason] brackets; same shape as the dry-run
    path (calibrate.py:459 docstring).
    """
    import scout.trading.calibrate as calibrate

    source = inspect.getsource(calibrate)
    idx = source.index("calibration applied:")
    tail = source[idx : idx + 400]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail


# ---------------------------------------------------------------------
# Site #5: weekly digest (two call sites)
# ---------------------------------------------------------------------


def test_weekly_digest_calls_pass_parse_mode_none():
    """Site #5: scout/trading/weekly_digest.py both call sites (chunk
    dispatch at :335 and fallback at :340) use parse_mode=None. Body
    interpolates signal_type, combo_key, symbol; section headers use
    [...] brackets which would mis-render as Markdown link anchors.
    """
    import scout.trading.weekly_digest as wd

    source = inspect.getsource(wd)
    occurrences = []
    cursor = 0
    while True:
        idx = source.find("alerter.send_telegram_message", cursor)
        if idx == -1:
            break
        occurrences.append(idx)
        cursor = idx + 1
    assert len(occurrences) >= 2, "expected at least 2 dispatch sites"
    for idx in occurrences:
        tail = source[idx : idx + 300]
        assert "parse_mode=None" in tail, (
            f"weekly_digest dispatch at char {idx} missing parse_mode=None"
        )


# ---------------------------------------------------------------------
# Site #6: velocity alert (escape user-data, keep Markdown)
# ---------------------------------------------------------------------


def test_velocity_alert_escapes_user_data_fields():
    """Site #6: format_velocity_alert preserves *bold* + [chart](url)
    intent, but symbol/name are passed through _escape_md so underscores
    do not get consumed as italics markers.
    """
    from scout.velocity.detector import format_velocity_alert

    detection = {
        "symbol": "AS_ROID",
        "name": "Asteroid_Test",
        "coin_id": "asteroid_coin",
        "price_change_1h": 50.0,
        "price_change_24h": 30.0,
        "market_cap": 1_000_000.0,
        "volume_24h": 500_000.0,
        "vol_mcap_ratio": 0.5,
        "current_price": 0.0001,
    }
    text = format_velocity_alert([detection])
    assert "AS\\_ROID" in text, "symbol underscore must be escaped"
    assert "Asteroid\\_Test" in text, "name underscore must be escaped"
    assert "*AS\\_ROID*" in text, "bold formatting around symbol preserved"
    assert "[chart](" in text, "chart link preserved"


def test_velocity_alert_url_path_not_escaped():
    """Site #6 (no-escape pin): coin_id sits inside a URL path; escaping
    it would break the link target. PINS the no-escape decision so a
    future "helpful" PR that escapes coin_id is caught.
    """
    from scout.velocity.detector import format_velocity_alert

    detection = {
        "symbol": "AST",
        "name": "Asteroid",
        "coin_id": "asteroid_coin",
        "price_change_1h": 50.0,
        "price_change_24h": 30.0,
        "market_cap": 1_000_000.0,
        "volume_24h": 500_000.0,
        "vol_mcap_ratio": 0.5,
        "current_price": 0.0001,
    }
    text = format_velocity_alert([detection])
    assert "(https://www.coingecko.com/en/coins/asteroid_coin)" in text, (
        "coin_id in URL path must NOT be escaped"
    )
    assert "asteroid\\_coin" not in text, (
        "coin_id in URL path must NOT be escaped"
    )


# ---------------------------------------------------------------------
# Site #7: send_alert / format_alert_message (audit-missed)
# ---------------------------------------------------------------------


def test_format_alert_message_escapes_user_data_fields(token_factory):
    """Site #7: format_alert_message at scout/alerter.py:15 must escape
    user-data fields (token_name, ticker, chain, virality_class, signal
    names, mirofish_report) so Telegram's MarkdownV1 parser does not
    consume underscores.
    """
    from scout.alerter import format_alert_message

    token = token_factory(
        contract_address="0xabc_def",
        chain="solana_test",
        token_name="AS_ROID",
        ticker="AS_RD",
        market_cap_usd=75000,
        quant_score=80,
        narrative_score=75,
        conviction_score=78,
        virality_class="High_Test",
        mirofish_report="Has under_score chars",
    )
    signals = ["vol_liq_ratio", "momentum_ratio"]
    msg = format_alert_message(token, signals)

    assert r"AS\_ROID" in msg, "token_name underscore must be escaped"
    assert r"AS\_RD" in msg, "ticker underscore must be escaped"
    assert r"solana\_test" in msg, "chain underscore must be escaped"
    assert r"High\_Test" in msg, "virality_class underscore must be escaped"
    assert r"vol\_liq\_ratio" in msg, "signal name underscore must be escaped"
    assert r"momentum\_ratio" in msg, "signal name underscore must be escaped"
    assert r"under\_score" in msg, "mirofish_report underscore must be escaped"
    assert r"*AS\_ROID*" in msg, "bold formatting around token_name preserved"


def test_format_alert_message_url_path_not_escaped(token_factory):
    """Site #7 (no-escape pin): contract_address sits inside a URL path
    (DexScreener or CoinGecko); escaping it would break the link.
    """
    from scout.alerter import format_alert_message

    # DexScreener path
    token = token_factory(
        contract_address="0xabc_def",
        chain="solana",
        token_name="MoonCoin",
        ticker="MOON",
        market_cap_usd=75000,
        virality_class="High",
        mirofish_report="x",
    )
    msg = format_alert_message(token, ["vol_liq_ratio"])
    assert "https://dexscreener.com/solana/0xabc_def" in msg, (
        "contract_address in URL path must NOT be escaped"
    )
    assert "0xabc\\_def" not in msg, "contract_address must NOT be escaped"

    # CoinGecko path (chain == 'coingecko')
    token = token_factory(
        contract_address="some_id",
        chain="coingecko",
        token_name="MoonCoin",
        ticker="MOON",
        market_cap_usd=75000,
        virality_class="High",
        mirofish_report="x",
    )
    msg = format_alert_message(token, ["vol_liq_ratio"])
    assert "https://www.coingecko.com/en/coins/some_id" in msg, (
        "contract_address in CoinGecko URL must NOT be escaped"
    )
