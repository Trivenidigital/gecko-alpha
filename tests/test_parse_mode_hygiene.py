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
# AST structural coverage — Layer 3
# ---------------------------------------------------------------------


SCOUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scout"


def _find_dispatch_calls(tree: ast.AST) -> list[ast.Call]:
    """Find every ast.Call to `send_telegram_message` in a parsed module.

    Matches both `send_telegram_message(...)` (attribute or name) and
    `alerter.send_telegram_message(...)`. Does NOT catch:
      - `from scout.alerter import send_telegram_message as stm; stm(...)`
      - `f = send_telegram_message; await f(...)` local-variable aliases
    Current tree has zero such patterns (verified by grep). If introduced,
    extend the walker accordingly.
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


def _resolve_string_literal(
    expr: ast.AST, name_map: dict[str, ast.AST]
) -> str | None:
    """Best-effort resolution of `expr` to its static-string content.
    Follows Name -> assigned-value through `name_map`. Returns None if
    the expression is not statically resolvable.
    """
    seen: set[int] = set()
    while True:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value
        if isinstance(expr, ast.JoinedStr):
            return "".join(
                seg.value
                for seg in expr.values
                if isinstance(seg, ast.Constant) and isinstance(seg.value, str)
            )
        if isinstance(expr, ast.Name) and expr.id in name_map:
            if id(expr) in seen:
                return None
            seen.add(id(expr))
            expr = name_map[expr.id]
            continue
        return None


def _resolve_dict(
    expr: ast.AST, name_map: dict[str, ast.AST]
) -> ast.Dict | None:
    """Resolve `expr` to an ast.Dict, following Name through name_map."""
    seen: set[int] = set()
    while True:
        if isinstance(expr, ast.Dict):
            return expr
        if isinstance(expr, ast.Name) and expr.id in name_map:
            if id(expr) in seen:
                return None
            seen.add(id(expr))
            expr = name_map[expr.id]
            continue
        return None


def _build_name_map(
    body: list[ast.stmt],
) -> dict[str, ast.AST]:
    """Build a {name: value} map from Assign statements in `body`.
    Only handles simple `name = expr` forms (Name target); skips tuples,
    attribute targets, augmented assignments, etc.
    """
    name_map: dict[str, ast.AST] = {}
    for stmt in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    name_map[target.id] = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                name_map[stmt.target.id] = stmt.value
    return name_map


def _find_direct_telegram_post_calls(tree: ast.AST) -> list[ast.Call]:
    """Find every `*.post(...)` call whose URL arg statically resolves to
    a string containing '/sendMessage'. Resolves variables through their
    enclosing-function assignments so that the canonical pattern
    `telegram_url = f"...sendMessage"; session.post(telegram_url, ...)`
    is caught -- this is the pattern that the original audit missed at
    scout/alerter.py:189 (send_alert).
    """
    calls: list[ast.Call] = []
    # Walk every function (sync + async); within each, build a name map
    # and resolve session.post URL args through it.
    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name_map = _build_name_map(func_node.body)
        for call in ast.walk(func_node):
            if not isinstance(call, ast.Call):
                continue
            cfunc = call.func
            if not isinstance(cfunc, ast.Attribute) or cfunc.attr != "post":
                continue
            # URL is positional[0] OR url= kwarg
            url_expr: ast.AST | None = None
            if call.args:
                url_expr = call.args[0]
            for kw in call.keywords:
                if kw.arg == "url":
                    url_expr = kw.value
                    break
            if url_expr is None:
                continue
            url_str = _resolve_string_literal(url_expr, name_map)
            if url_str and "/sendMessage" in url_str:
                calls.append(call)
    return calls


def _direct_post_has_parse_mode(
    call: ast.Call, name_map: dict[str, ast.AST]
) -> bool:
    """Return True if a session.post(...) call carries parse_mode either
    as a direct kwarg or inside its `json=`/`data=` dict (resolved through
    name_map). Note: name_map must come from the SAME function scope as
    the call.
    """
    payload_expr: ast.AST | None = None
    for kw in call.keywords:
        if kw.arg == "parse_mode":
            return True
        if kw.arg in ("json", "data"):
            payload_expr = kw.value
    if payload_expr is None:
        return False
    payload_dict = _resolve_dict(payload_expr, name_map)
    if payload_dict is None:
        return False
    for k in payload_dict.keys:
        if isinstance(k, ast.Constant) and k.value == "parse_mode":
            return True
    return False


# Allowlist of currently-known sites that DO NOT explicitly pin parse_mode.
# BL-NEW-PARSE-MODE-AUDIT scope only fixes the 7 HIGH ACTUAL sites. The
# 10 sites below fall into:
#  - send_telegram_message dispatch sites (LOW/MEDIUM per audit, body
#    shape unlikely to mangle):
#      scout/chains/alerts.py:59
#      scout/trading/suppression.py:186
#      scout/live/loops.py:251
#      scout/main.py:166 (combo_refresh failure)
#      scout/social/lunarcrush/alerter.py:144 (body uses _escape_md)
#  - send_telegram_message dispatch sites (HIGH POTENTIAL per audit,
#    deferred pending 7-day production log review):
#      scout/main.py:351 (briefing chunked summary)
#      scout/main.py:434 (counter-arg follow-up)
#      scout/main.py:1537 (daily summary)
#  - direct session.post(.../sendMessage) sites (caught by the resolver-
#    aware second AST arm — both are intentionally parse_mode-less):
#      scout/alerter.py:162 — INSIDE send_telegram_message itself; the
#        function takes parse_mode as a parameter and adds it to the
#        payload conditionally (`if parse_mode is not None: payload[...]
#        = parse_mode`). The walker can't see the dynamic dict insert,
#        but the function's caller-passes-kwarg contract is the design
#        intent and is enforced by every CALL site (which the first AST
#        arm checks). Self-referential allowlist entry.
#      scout/social/telegram/listener.py:123 — intentional plain-text
#        Telegram dispatch (channel-listener auth flow). Payload at
#        :120-121 builds {chat_id, text} with NO parse_mode field;
#        Telegram defaults to plain text. Per plan-review reviewer A:
#        "SAFE by construction."
# Follow-up PRs remove entries from this set; a NEW dispatch site that's
# not in this allowlist will be caught at CI time.
_ALLOWLIST_DISPATCH_SITES_WITHOUT_PARSE_MODE: set[tuple[str, int]] = {
    ("scout/chains/alerts.py", 59),
    ("scout/trading/suppression.py", 186),
    ("scout/live/loops.py", 251),
    ("scout/main.py", 166),
    ("scout/social/lunarcrush/alerter.py", 144),
    ("scout/main.py", 351),
    ("scout/main.py", 434),
    ("scout/main.py", 1537),
    ("scout/alerter.py", 162),
    ("scout/social/telegram/listener.py", 123),
}


def test_all_dispatch_sites_pin_parse_mode():
    """Layer 3: every send_telegram_message call site in scout/ MUST pass
    parse_mode explicitly (None or "Markdown" or "MarkdownV2" or "HTML").

    Rationale: the original audit grepped `send_telegram_message` source
    occurrences and missed `send_alert` at scout/alerter.py:189 because
    that function does its own session.post call. An AST walk catches every
    dispatch regardless of formatting, multi-line layout, or kwarg-from-
    variable. Closes the audit-methodology gap so a NEW dispatch site added
    6 months from now without parse_mode= is caught at CI time.

    Allowlist: this PR scopes only the 7 HIGH ACTUAL sites; deferred sites
    (LOW/MEDIUM + HIGH POTENTIAL per the audit) live in
    _ALLOWLIST_DISPATCH_SITES_WITHOUT_PARSE_MODE. Follow-up PRs remove
    entries as those sites are fixed.
    """
    offenders: list[str] = []
    for py_path in SCOUT_DIR.rglob("*.py"):
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel_path = str(py_path.relative_to(SCOUT_DIR.parent)).replace("\\", "/")
        for call in _find_dispatch_calls(tree):
            kwarg_names = {kw.arg for kw in call.keywords}
            if "parse_mode" in kwarg_names:
                continue
            site = (rel_path, call.lineno)
            if site in _ALLOWLIST_DISPATCH_SITES_WITHOUT_PARSE_MODE:
                continue
            offenders.append(
                f"{rel_path}:{call.lineno} send_telegram_message() "
                f"without parse_mode= kwarg (and not allowlisted)"
            )
        # Second arm: direct session.post(...) hits to Telegram
        # /sendMessage. The body must contain parse_mode if it intends
        # Markdown rendering; if absent, the dispatch is plain-text
        # (Telegram default) and safe. Resolver-aware: follows variable
        # assignments through the enclosing function scope so the
        # canonical pattern `payload = {...}; session.post(url, json=payload)`
        # is caught -- this is the pattern the original audit missed at
        # scout/alerter.py:189.
        for func_node in ast.walk(tree):
            if not isinstance(
                func_node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            name_map = _build_name_map(func_node.body)
            for call in ast.walk(func_node):
                if not isinstance(call, ast.Call):
                    continue
                cfunc = call.func
                if (
                    not isinstance(cfunc, ast.Attribute)
                    or cfunc.attr != "post"
                ):
                    continue
                # Resolve URL arg
                url_expr: ast.AST | None = None
                if call.args:
                    url_expr = call.args[0]
                for kw in call.keywords:
                    if kw.arg == "url":
                        url_expr = kw.value
                        break
                if url_expr is None:
                    continue
                url_str = _resolve_string_literal(url_expr, name_map)
                if not (url_str and "/sendMessage" in url_str):
                    continue
                # Telegram dispatch -- check allowlist + parse_mode resolution
                site = (rel_path, call.lineno)
                if site in _ALLOWLIST_DISPATCH_SITES_WITHOUT_PARSE_MODE:
                    continue
                if _direct_post_has_parse_mode(call, name_map):
                    continue
                offenders.append(
                    f"{rel_path}:{call.lineno} direct session.post(.../sendMessage) "
                    f"without parse_mode in payload (and not allowlisted)"
                )
    assert not offenders, (
        "Telegram dispatch sites missing parse_mode "
        "(see BL-NEW-PARSE-MODE-AUDIT + CLAUDE.md §12b):\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------
# Site #1: narrative heating alert
# ---------------------------------------------------------------------


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
    # Look BEFORE+AFTER the format-call anchor since send_telegram_message
    # is now in a multi-line wrap that puts it BEFORE the format call.
    idx = source.index("format_secondwave_alert(")
    window = source[max(0, idx - 200) : idx + 400]
    assert "send_telegram_message(" in window
    assert "parse_mode=None" in window


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
    # Search a window BEFORE+AFTER the body string anchor; the dispatch
    # wraps the body, so send_telegram_message( appears BEFORE the anchor.
    idx = source.index("calibration applied:")
    window = source[max(0, idx - 200) : idx + 400]
    assert "send_telegram_message(" in window
    assert "parse_mode=None" in window


# ---------------------------------------------------------------------
# Site #5: weekly digest (two call sites)
# ---------------------------------------------------------------------


def test_weekly_digest_calls_pass_parse_mode_none():
    """Site #5: scout/trading/weekly_digest.py both call sites (chunk
    dispatch at :335 and fallback at :340) use parse_mode=None. Body
    interpolates signal_type, combo_key, symbol; section headers use
    [...] brackets which would mis-render as Markdown link anchors.

    Uses AST to find actual dispatch Call nodes (avoids false matches
    against docstring text that mentions send_telegram_message).
    """
    import scout.trading.weekly_digest as wd

    source = inspect.getsource(wd)
    tree = ast.parse(source)
    calls = _find_dispatch_calls(tree)
    assert len(calls) >= 2, f"expected >= 2 dispatch Call nodes, got {len(calls)}"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "parse_mode" in kwargs, (
            f"weekly_digest dispatch at line {call.lineno} missing parse_mode= kwarg"
        )
        # The kwarg value must be None (a Constant node with value=None)
        pm = next(kw for kw in call.keywords if kw.arg == "parse_mode")
        assert isinstance(pm.value, ast.Constant) and pm.value.value is None, (
            f"weekly_digest dispatch at line {call.lineno} parse_mode value "
            f"is not None"
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
    assert "[chart](https://dexscreener.com/solana/0xabc_def)" in msg, (
        "URL emitted as [chart](url) link with raw contract_address inside parens"
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
    assert "[chart](https://www.coingecko.com/en/coins/some_id)" in msg, (
        "CoinGecko URL emitted as [chart](url) link with raw contract_address inside parens"
    )


# ---------------------------------------------------------------------
# Wire-level integration tests (both primitives)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_with_parse_mode_none_omits_parse_mode_from_payload(
    settings_factory,
):
    """Wire-level: when parse_mode=None is passed to send_telegram_message,
    the JSON payload posted to Telegram does NOT include a parse_mode field
    (per scout/alerter.py:143-144). Layer 4 pin behind Layer 2 source-pins
    for sites #1-5.
    """
    import aiohttp
    from aioresponses import aioresponses

    from scout.alerter import send_telegram_message

    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="test-chat",
    )
    captured_payload: dict = {}

    async def _callback(url, **kwargs):
        captured_payload.update(kwargs.get("json", {}))

    with aioresponses() as m:
        m.post(
            "https://api.telegram.org/bottest-token/sendMessage",
            payload={"ok": True},
            callback=_callback,
        )
        async with aiohttp.ClientSession() as session:
            await send_telegram_message(
                "gainers_early alert: AS_ROID up 50%",
                session,
                settings,
                parse_mode=None,
            )

    assert "parse_mode" not in captured_payload, (
        "parse_mode=None caller must NOT set the parse_mode JSON field"
    )
    assert captured_payload["text"] == "gainers_early alert: AS_ROID up 50%"


@pytest.mark.asyncio
async def test_dispatch_with_parse_mode_markdown_sends_escaped_payload(
    settings_factory,
):
    """Wire-level: when parse_mode='Markdown' is passed and the caller has
    already _escape_md-ed user-data fields, the payload carries the escaped
    form AND parse_mode=Markdown. Layer 4 pin for sites #6, #7.
    """
    import aiohttp
    from aioresponses import aioresponses

    from scout.alerter import _escape_md, send_telegram_message

    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="test-chat",
    )
    captured_payload: dict = {}

    async def _callback(url, **kwargs):
        captured_payload.update(kwargs.get("json", {}))

    with aioresponses() as m:
        m.post(
            "https://api.telegram.org/bottest-token/sendMessage",
            payload={"ok": True},
            callback=_callback,
        )
        async with aiohttp.ClientSession() as session:
            body = f"*{_escape_md('AS_ROID')}* alert"
            await send_telegram_message(body, session, settings)

    assert captured_payload["parse_mode"] == "Markdown"
    assert "AS\\_ROID" in captured_payload["text"], (
        "user-data field must be wire-level escaped"
    )
    assert "*AS\\_ROID*" in captured_payload["text"], (
        "intentional Markdown bold must reach the wire"
    )


# ---------------------------------------------------------------------
# End-to-end wire-level send_alert test (Reviewer 1 C6)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_alert_wire_level_escapes_user_data(
    token_factory, settings_factory,
):
    """Site #7 end-to-end: send_alert -> format_alert_message ->
    session.post(.../sendMessage). The full path. Body composer escapes
    user-data fields, dispatch keeps parse_mode='Markdown'. Pins the
    primary candidate-alert path that the original audit missed.
    """
    import aiohttp
    from aioresponses import aioresponses

    from scout.alerter import send_alert

    token = token_factory(
        contract_address="0xabc_def",
        chain="solana_test",
        token_name="AS_ROID",
        ticker="AS_RD",
        market_cap_usd=75000,
        quant_score=80,
        narrative_score=75,
        conviction_score=78,
        virality_class="High",
        mirofish_report="Has under_score text",
    )
    signals = ["vol_liq_ratio", "momentum_ratio"]
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="test-chat",
        DISCORD_WEBHOOK_URL="",
    )
    captured: dict = {}

    async def _callback(url, **kwargs):
        captured.update(kwargs.get("json", {}))

    with aioresponses() as m:
        m.post(
            "https://api.telegram.org/bottest-token/sendMessage",
            payload={"ok": True},
            callback=_callback,
        )
        async with aiohttp.ClientSession() as session:
            await send_alert(token, signals, session, settings)

    assert captured.get("parse_mode") == "Markdown"
    body = captured["text"]
    assert "AS\\_ROID" in body, "token_name escaped at wire"
    assert "AS\\_RD" in body, "ticker escaped at wire"
    assert "solana\\_test" in body, "chain escaped at wire"
    assert "vol\\_liq\\_ratio" in body, "signal name escaped at wire"
    assert "momentum\\_ratio" in body, "signal name escaped at wire"
    assert "under\\_score" in body, "mirofish_report escaped at wire"
    assert "*AS\\_ROID*" in body, "intentional bold preserved at wire"
    assert "[chart](https://dexscreener.com/solana_test/0xabc_def)" in body, (
        "URL emitted as [chart](url) link with raw contract_address"
    )
