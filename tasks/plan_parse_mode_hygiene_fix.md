**New primitives introduced:** NONE — per-site application of existing `parse_mode=None` and `_escape_md` primitives from `scout/alerter.py`.

# Parse-Mode Hygiene Fix Implementation Plan (BL-NEW-PARSE-MODE-AUDIT follow-up)

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Execute one task at a time, commit after each, run the full test suite between tasks.

**Goal:** Eliminate the 6 HIGH ACTUAL Class-3 silent-rendering-corruption sites confirmed in `tasks/findings_parse_mode_audit_2026_05_12.md` by applying the right hygiene primitive at each call site.

**Architecture:** Two existing primitives, applied per-site based on whether the message intends Markdown formatting:
- `parse_mode=None` — for system-health / digest / plain-text alerts. 5 of 6 sites use this.
- `_escape_md(value)` — for intentionally-formatted alerts (bold/links) where only the interpolated user-data field needs escaping. 1 of 6 sites (velocity) uses this.

**Tech Stack:** Python 3.11, pytest-asyncio, aioresponses for HTTP mocks. No new dependencies.

**Branch:** `fix/parse-mode-hygiene-class-3-audit-followup` (already created from master at `e1f501f`).

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram parse_mode hygiene | none found | Use in-tree `_escape_md` + `parse_mode=None` (both already in `scout/alerter.py`); same patterns already deployed at `scout/trading/auto_suspend.py:266,322`, `scout/trading/tg_alert_dispatch.py:311`, `scout/main.py:251,1007,1067,1205`, `scout/social/lunarcrush/alerter.py:144`. |
| Markdown-injection escaping | none found | Same as above — `_escape_md` covers `\ _ * [ ] \``. |

Awesome-hermes-agent ecosystem check: no Telegram-alert-rendering skill exists. Verdict: in-tree primitives are the canonical fix; build nothing new.

## Drift-check

Searched `scout/` for sites already fixed:
- `scout/trading/auto_suspend.py:266,322` — `parse_mode=None` ✓ (PR #79, 2026-05-06)
- `scout/trading/tg_alert_dispatch.py:311` — `parse_mode=None` ✓
- `scout/main.py:251,1007,1067,1205` — `parse_mode=None` ✓ (per audit "ALREADY SAFE" lane)
- `scout/social/lunarcrush/alerter.py:144` — `_escape_md` ✓ (audit #15)

The 6 HIGH ACTUAL sites remain unfixed. Audit doc is current as of 2026-05-12; no prior PR has closed them.

## Reviewer-discovered scope expansion (Plan-review round 1, 2026-05-13)

Plan-review round 1 (Reviewer A, scope/coverage vector) discovered an additional HIGH ACTUAL site that the audit had missed: `scout/alerter.py:189 send_alert` — the **primary candidate-alert path** (called from `scout/main.py:871` on every gate pass). The audit grepped `send_telegram_message` only; `send_alert` is a separate function that calls Telegram directly via `session.post` with hardcoded `"parse_mode": "Markdown"` at line 209. Body `format_alert_message` (lines 15-72) interpolates raw `token.token_name`, `token.ticker`, `token.chain`, signal names (every gecko-alpha signal_type contains underscores), and `token.mirofish_report` (LLM output). This is the highest-volume mangling path in the system; every primary candidate alert has been silently mangling its `Signals:` line and any LLM-special-char content for the lifetime of the codebase.

Fix shape: same as velocity (#6) — author intent is Markdown (`*token_name*` bold wrapper at line 21, `[chart]({url})` link patterns in other formatters); keep `parse_mode=Markdown` at line 209 and `_escape_md` the interpolated user-data fields in `format_alert_message`.

**Total in-scope HIGH ACTUAL sites in this PR: 7** (6 from audit + 1 from plan review).

## Per-site fix matrix

| # | Site | Current state | Fix primitive | Why this primitive |
|---|---|---|---|---|
| 1 | `scout/narrative/agent.py:557` (call) + `scout/narrative/digest.py:format_heating_alert` (body) | default `parse_mode=Markdown`; body has NO `*`/`[`/`` ` `` formatters | `parse_mode=None` at call site | Body is plain text; no formatting intent to preserve |
| 2 | `scout/narrative/agent.py:715` (call) + `scout/trading/digest.py:build_paper_digest` (body) | default `parse_mode=Markdown`; body has NO formatters; body interpolates `best_symbol`, `worst_symbol`, AND per-signal `{sig}` (every signal_type has underscores) | `parse_mode=None` at call site | Body is plain text; signal_type interpolation mangles on every digest |
| 3 | `scout/secondwave/detector.py:285` (call) + `scout/secondwave/alerts.py:format_secondwave_alert` (body) | default `parse_mode=Markdown`; body has NO formatters; interpolates `token_name`, `ticker`, peak_signals join, reacc_signals join | `parse_mode=None` at call site | Body is plain text; 4 vulnerable fields per alert |
| 4 | `scout/trading/calibrate.py:354` (apply path) | default `parse_mode=Markdown`; body interpolates `d.signal_type` (always has `_`), `c.field`, `d.reason` inside `[…]` brackets | `parse_mode=None` at call site | Already-documented inconsistency: dry-run path uses `parse_mode=None` (per docstring at `calibrate.py:459` and `alerter.py:131-135`), apply path doesn't. Same `[reason]` shape → same fix. |
| 5 | `scout/trading/weekly_digest.py:335,340` (both call sites in same function) | default `parse_mode=Markdown`; body interpolates `e["symbol"]`, `sig` (signal_type), `r["combo_key"]`, plus `[…]` section markers | `parse_mode=None` at both call sites | System-health digest; brackets are section delimiters, not Markdown |
| 6 | `scout/velocity/detector.py:193` (call) + `scout/velocity/detector.py:159-177:format_velocity_alert` (body) | default `parse_mode=Markdown`; body INTENTIONALLY uses `*bold*` and `[chart](url)` link formatting; interpolates `det['symbol']`, `det['name']` raw | `_escape_md(det['symbol'])` + `_escape_md(det['name'])` in `format_velocity_alert`; keep `parse_mode=Markdown` | Author intended Markdown rendering (bold/clickable links); escape only the user-data fields |
| 7 | `scout/alerter.py:189` (`send_alert` function) + `scout/alerter.py:15-72` (`format_alert_message` body) | hardcoded `"parse_mode": "Markdown"` at line 209; body intentionally uses `*{token_name}*` bold; interpolates `token_name`, `ticker`, `chain`, signal names (join), `mirofish_report` raw | `_escape_md(...)` on each user-data field in `format_alert_message`; keep `parse_mode=Markdown` in `send_alert` payload | Primary candidate-alert path. Author intent was Markdown bold for token name. Same pattern as velocity: keep Markdown, escape interpolated fields. (Discovered by plan review — audit missed because it grepped `send_telegram_message` only.) |

## File structure

**Files to modify (production code):**
- `scout/narrative/agent.py` — add `parse_mode=None` at line 557
- `scout/narrative/agent.py` — add `parse_mode=None` at line 715
- `scout/secondwave/detector.py` — add `parse_mode=None` at line 285
- `scout/trading/calibrate.py` — add `parse_mode=None` at line 354
- `scout/trading/weekly_digest.py` — add `parse_mode=None` at lines 335 and 340
- `scout/velocity/detector.py` — wrap `det['symbol']` and `det['name']` with `_escape_md(...)` in `format_velocity_alert`
- `scout/alerter.py` — wrap `token.token_name`, `token.ticker`, `token.chain`, signal names (in the `", ".join(signals)`), and `token.mirofish_report` with `_escape_md(...)` in `format_alert_message` (lines 15-72); keep hardcoded `parse_mode=Markdown` at line 209 (intentional)

**Files to modify (test code — for kwarg compatibility):**
- `tests/test_trading_weekly_digest.py:276,347` — update `_capture(text, session, settings)` to `_capture(text, session, settings, **kwargs)` to accept the new `parse_mode=None` kwarg
- `tests/test_alerter.py:46` — update assertion from `assert "vol_liq_ratio" in msg` to `assert r"vol\_liq\_ratio" in msg` (signal names now escaped under format_alert_message)

**Files to create (tests):**
- `tests/test_parse_mode_hygiene.py` — regression tests covering all 7 sites + a structural test that walks `scout/` for any future site missing the hygiene primitive.

**Files NOT to touch in this PR (out of scope):**
- `scout/main.py:350,433,1521` — 3 HIGH POTENTIAL sites. Per audit §"What's NOT in this audit" point 3, these need post-deploy log observation first; deferred to a separate follow-up PR after a 7-day soak. Plan-review reviewer A suggested promoting `:434` (counter-arg) to HIGH ACTUAL on the basis of `[HIGH]`/`[CRITICAL]` bracket markdown-link-anchor risk. **Rejected after verification:** Telegram MarkdownV1 link parsing requires `[label](url)` adjacency; the body at line 425 has `[{severity}] {flag}: {detail}` where the bracket is followed by whitespace + text, not `(`. Bare `[HIGH]` renders as literal text. Underlying ticker/LLM-output Markdown-special-char risk matches the audit's HIGH POTENTIAL classification — defer per audit policy.
- The 5 LOW/MEDIUM sites — body shape unlikely to mangle; per audit, no change needed.
- `scout/narrative/digest.py:format_daily_digest` — verified by plan review: zero callers in `scout/` (grep). Dead-path; safe to defer.
- `tests/test_trading_suppression.py:321` `_capture` — uses positional-only signature too, BUT this PR does not modify `scout/trading/suppression.py:186` dispatch, so the test mock signature does not need updating in this PR.

## Self-review notes (in advance)

1. **Audit field coverage is partial.** The audit lists "best_symbol, worst_symbol" for site #2, but `build_paper_digest` also interpolates `{sig}` (signal_type) per-line. `parse_mode=None` fixes both — no per-field analysis needed because the whole-body parse mode is disabled. ✓
2. **Velocity escape coverage.** `_escape_md` covers `\ _ * [ ] \``. Token symbols and names in practice contain underscores (`AS_ROID`) and occasionally brackets/asterisks. The url and other markdown intent stays. ✓
3. **Calibrate dry-run inconsistency.** The apply path at line 354 was overlooked when the dry-run path was fixed per silent-failure C1. This PR closes the gap. ✓

---

## Task 1: Add structural test that catches future regressions

**Files:**
- Create: `tests/test_parse_mode_hygiene.py`

This task creates the test scaffold and one safety-net structural test. Per-site tests follow in Tasks 2-7.

- [ ] **Step 1.1: Write the structural-coverage test module scaffold**

Create `tests/test_parse_mode_hygiene.py`:

```python
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
```

**Note:** This AST test is intentionally written FIRST (Task 1) and will FAIL until all 7 sites are fixed. Each subsequent task's commit moves one offender off the list. The test reaching PASS state at end of Task 8 is the integration-coverage proof.

- [ ] **Step 1.2: Run to verify the AST test fails with the expected offender count**

Run: `uv run pytest tests/test_parse_mode_hygiene.py::test_all_dispatch_sites_pin_parse_mode -v`
Expected: FAIL. The failure message should list **exactly the 6 send_telegram_message dispatch sites** without `parse_mode=` from the audit (sites #1-5; weekly_digest has 2). The 7th site (`send_alert` at `alerter.py:189`) is NOT caught by this AST test because `send_alert` does its own `session.post` — it's caught by Task 8's render test instead.

If the offender list contains MORE than 6 entries, an additional dispatch site exists that the audit missed and this PR will close at the same time — flag it but proceed (broader coverage is good).

- [ ] **Step 1.3: Commit**

```bash
git add tests/test_parse_mode_hygiene.py
git commit -m "test(parse-mode): AST coverage test + scaffold for BL-NEW-PARSE-MODE-AUDIT"
```

---

## Task 2: Fix site #1 — narrative heating alert

**Files:**
- Modify: `scout/narrative/agent.py:557`
- Test: `tests/test_parse_mode_hygiene.py`

- [ ] **Step 2.1: Write failing test**

Append to `tests/test_parse_mode_hygiene.py`:

```python
@pytest.mark.asyncio
async def test_narrative_heating_alert_uses_plain_text(monkeypatch):
    """Site #1: format_heating_alert dispatch must use parse_mode=None.

    Body interpolates p.symbol from predictions; symbols like 'AS_ROID'
    silently mangle under default Markdown parse mode.
    """
    from scout.narrative.digest import format_heating_alert
    from scout.narrative.models import CategoryAcceleration, NarrativePrediction

    # Build a synthetic prediction with an underscored symbol
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
    # The body itself must contain the raw underscored symbol — escaping is
    # the call site's job (via parse_mode=None), NOT the formatter's job
    assert "AS_ROID" in text
```

Then add a call-site contract test:

```python
def test_narrative_agent_alert_call_passes_parse_mode_none():
    """Site #1 call-site contract: scout/narrative/agent.py:557 dispatches
    with parse_mode=None. This is a source-level pin — if a future refactor
    removes the kwarg, the test fails."""
    import inspect
    import scout.narrative.agent as agent

    source = inspect.getsource(agent)
    # The send_telegram_message call inside the heating-alert block must
    # carry parse_mode=None
    # (Source-level assertion — exact-line pinning would be brittle to
    # refactors; substring assertion is sufficient for regression catch.)
    assert "format_heating_alert(" in source
    # Find the dispatch line near the format_heating_alert call
    idx = source.index("format_heating_alert(")
    # Look ahead at most 600 chars for the send call + parse_mode
    tail = source[idx : idx + 600]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `uv run pytest tests/test_parse_mode_hygiene.py::test_narrative_agent_alert_call_passes_parse_mode_none -v`
Expected: FAIL — `parse_mode=None` not yet in source.

- [ ] **Step 2.3: Apply fix**

Edit `scout/narrative/agent.py` lines 557-559. Change:

```python
                                    await send_telegram_message(
                                        alert_text, session, settings
                                    )
```

to:

```python
                                    await send_telegram_message(
                                        alert_text, session, settings, parse_mode=None
                                    )
```

- [ ] **Step 2.4: Run test to verify it passes**

Run: `uv run pytest tests/test_parse_mode_hygiene.py -v`
Expected: PASS.

- [ ] **Step 2.5: Commit**

```bash
git add scout/narrative/agent.py tests/test_parse_mode_hygiene.py
git commit -m "fix(narrative): heating alert dispatches parse_mode=None (BL-NEW-PARSE-MODE-AUDIT site #1)"
```

---

## Task 3: Fix site #2 — paper trading daily digest

**Files:**
- Modify: `scout/narrative/agent.py:715`
- Test: `tests/test_parse_mode_hygiene.py`

- [ ] **Step 3.1: Write failing test**

Append to `tests/test_parse_mode_hygiene.py`:

```python
def test_paper_digest_call_passes_parse_mode_none():
    """Site #2: scout/narrative/agent.py:715 dispatches paper digest
    with parse_mode=None. Body interpolates best_symbol/worst_symbol AND
    per-signal_type keys; every signal_type has underscores."""
    import inspect
    import scout.narrative.agent as agent

    source = inspect.getsource(agent)
    idx = source.index("build_paper_digest")
    tail = source[idx : idx + 800]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `uv run pytest tests/test_parse_mode_hygiene.py::test_paper_digest_call_passes_parse_mode_none -v`
Expected: FAIL.

- [ ] **Step 3.3: Apply fix**

Edit `scout/narrative/agent.py` lines 715-717. Change:

```python
                                await send_telegram_message(
                                    digest_text, session, settings
                                )
```

to:

```python
                                await send_telegram_message(
                                    digest_text, session, settings, parse_mode=None
                                )
```

- [ ] **Step 3.4: Run test to verify it passes**

Run: `uv run pytest tests/test_parse_mode_hygiene.py -v`
Expected: PASS.

- [ ] **Step 3.5: Commit**

```bash
git add scout/narrative/agent.py tests/test_parse_mode_hygiene.py
git commit -m "fix(narrative): paper digest dispatches parse_mode=None (BL-NEW-PARSE-MODE-AUDIT site #2)"
```

---

## Task 4: Fix site #3 — secondwave alert

**Files:**
- Modify: `scout/secondwave/detector.py:285`
- Test: `tests/test_parse_mode_hygiene.py`

- [ ] **Step 4.1: Write failing test**

Append:

```python
def test_secondwave_alert_call_passes_parse_mode_none():
    """Site #3: scout/secondwave/detector.py:285 dispatches with parse_mode=None.
    Body interpolates ticker, token_name, peak_signals join, reacc_signals join."""
    import inspect
    import scout.secondwave.detector as detector

    source = inspect.getsource(detector)
    idx = source.index("format_secondwave_alert(")
    tail = source[idx : idx + 400]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail
```

- [ ] **Step 4.2: Run test to verify it fails**

Run: `uv run pytest tests/test_parse_mode_hygiene.py::test_secondwave_alert_call_passes_parse_mode_none -v`
Expected: FAIL.

- [ ] **Step 4.3: Apply fix**

Edit `scout/secondwave/detector.py` line 285. Change:

```python
        await send_telegram_message(format_secondwave_alert(sw), session, settings)
```

to:

```python
        await send_telegram_message(
            format_secondwave_alert(sw), session, settings, parse_mode=None
        )
```

- [ ] **Step 4.4: Run test to verify it passes**

Run: `uv run pytest tests/test_parse_mode_hygiene.py -v`
Expected: PASS.

- [ ] **Step 4.5: Commit**

```bash
git add scout/secondwave/detector.py tests/test_parse_mode_hygiene.py
git commit -m "fix(secondwave): alert dispatch parse_mode=None (BL-NEW-PARSE-MODE-AUDIT site #3)"
```

---

## Task 5: Fix site #4 — calibration apply alert

**Files:**
- Modify: `scout/trading/calibrate.py:354`
- Test: `tests/test_parse_mode_hygiene.py`

- [ ] **Step 5.1: Write failing test**

Append:

```python
def test_calibrate_apply_alert_call_passes_parse_mode_none():
    """Site #4: scout/trading/calibrate.py:354 (apply path) dispatches
    with parse_mode=None. Body interpolates d.signal_type (always has
    underscores) inside [reason] brackets — same shape as dry-run path
    which already uses parse_mode=None (per calibrate.py:459 docstring)."""
    import inspect
    import scout.trading.calibrate as calibrate

    source = inspect.getsource(calibrate)
    # Find the apply-path dispatch — the one inside the `if session is not None
    # and not force_no_alert:` block
    idx = source.index("calibration applied:")
    tail = source[idx : idx + 400]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail
```

- [ ] **Step 5.2: Run test to verify it fails**

Run: `uv run pytest tests/test_parse_mode_hygiene.py::test_calibrate_apply_alert_call_passes_parse_mode_none -v`
Expected: FAIL.

- [ ] **Step 5.3: Apply fix**

Edit `scout/trading/calibrate.py` lines 354-358. Change:

```python
            await alerter.send_telegram_message(
                f"calibration applied:\n{summary}",
                session,
                settings,
            )
```

to:

```python
            await alerter.send_telegram_message(
                f"calibration applied:\n{summary}",
                session,
                settings,
                parse_mode=None,
            )
```

- [ ] **Step 5.4: Run test to verify it passes**

Run: `uv run pytest tests/test_parse_mode_hygiene.py -v`
Expected: PASS.

- [ ] **Step 5.5: Commit**

```bash
git add scout/trading/calibrate.py tests/test_parse_mode_hygiene.py
git commit -m "fix(calibrate): apply-path alert parse_mode=None — matches dry-run (BL-NEW-PARSE-MODE-AUDIT site #4)"
```

---

## Task 6: Fix site #5 — weekly digest (both call sites)

**Files:**
- Modify: `scout/trading/weekly_digest.py:335` and `:340`
- Test: `tests/test_parse_mode_hygiene.py`

- [ ] **Step 6.1: Write failing test**

Append:

```python
def test_weekly_digest_calls_pass_parse_mode_none():
    """Site #5: scout/trading/weekly_digest.py — both call sites (chunk
    dispatch at :335 and fallback at :340) use parse_mode=None. Body
    interpolates signal_type, combo_key, symbol; section headers use
    [...] brackets which would mis-render as Markdown link anchors."""
    import inspect
    import scout.trading.weekly_digest as wd

    source = inspect.getsource(wd)
    # Two distinct call sites — assert both
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
```

- [ ] **Step 6.2: Run test to verify it fails**

Run: `uv run pytest tests/test_parse_mode_hygiene.py::test_weekly_digest_calls_pass_parse_mode_none -v`
Expected: FAIL.

- [ ] **Step 6.3: Update test mock signatures to accept kwargs**

Plan-review reviewer B flagged: `tests/test_trading_weekly_digest.py:276` and `:347` define `async def _capture(text, session, settings):` — positional-only, no `**kwargs`. After we add `parse_mode=None` to the dispatch call, the mock will raise `TypeError: _capture() got an unexpected keyword argument 'parse_mode'`.

Edit `tests/test_trading_weekly_digest.py:276` from:

```python
    async def _capture(text, session, settings):
        sent.append(text)
```

to:

```python
    async def _capture(text, session, settings, **kwargs):
        sent.append(text)
```

Apply the **same edit** to the second `_capture` definition at `tests/test_trading_weekly_digest.py:347`.

- [ ] **Step 6.4: Apply fix at line 335**

Edit `scout/trading/weekly_digest.py` line 335. Change:

```python
            for chunk in chunks:
                await alerter.send_telegram_message(chunk, session, settings)
```

to:

```python
            for chunk in chunks:
                await alerter.send_telegram_message(
                    chunk, session, settings, parse_mode=None
                )
```

- [ ] **Step 6.5: Apply fix at line 340**

In the same file, change:

```python
            try:
                await alerter.send_telegram_message(
                    f"Weekly digest failed: {type(e).__name__} [ref={corr}]. Check logs.",
                    session,
                    settings,
                )
```

to:

```python
            try:
                await alerter.send_telegram_message(
                    f"Weekly digest failed: {type(e).__name__} [ref={corr}]. Check logs.",
                    session,
                    settings,
                    parse_mode=None,
                )
```

- [ ] **Step 6.6: Run test to verify it passes + verify existing weekly_digest tests still pass**

Run: `uv run pytest tests/test_parse_mode_hygiene.py tests/test_trading_weekly_digest.py -v`
Expected: PASS (the existing weekly_digest tests will catch the mock-signature TypeError if step 6.3 was skipped).

- [ ] **Step 6.7: Commit**

```bash
git add scout/trading/weekly_digest.py tests/test_trading_weekly_digest.py tests/test_parse_mode_hygiene.py
git commit -m "fix(weekly_digest): both dispatches use parse_mode=None + update mock sigs (BL-NEW-PARSE-MODE-AUDIT site #5)"
```

---

## Task 7: Fix site #6 — velocity alert (keep Markdown, escape user-data)

**Files:**
- Modify: `scout/velocity/detector.py:159-177` (`format_velocity_alert`)
- Test: `tests/test_parse_mode_hygiene.py`

Velocity is the ONLY site that intentionally uses Markdown (`*bold*` for emphasis, `[chart](url)` for clickable link). Fix is `_escape_md` on user-data fields, NOT `parse_mode=None`.

- [ ] **Step 7.1: Write failing tests (render + URL-preservation pin)**

Append:

```python
def test_velocity_alert_escapes_user_data_fields():
    """Site #6: format_velocity_alert preserves *bold* + [chart](url) intent,
    but symbol/name are passed through _escape_md so underscores don't get
    consumed as italics markers.
    """
    from scout.velocity.detector import format_velocity_alert

    detection = {
        "symbol": "AS_ROID",
        "name": "Asteroid_Test",
        "coin_id": "asteroid_coin",  # NOTE underscored coin_id — URL must NOT escape
        "price_change_1h": 50.0,
        "price_change_24h": 30.0,
        "market_cap": 1_000_000.0,
        "volume_24h": 500_000.0,
        "vol_mcap_ratio": 0.5,
        "current_price": 0.0001,
    }
    text = format_velocity_alert([detection])
    # The user-data fields must appear ESCAPED (with backslash before _)
    assert "AS\\_ROID" in text, (
        "symbol underscore must be escaped before Markdown rendering"
    )
    assert "Asteroid\\_Test" in text, (
        "name underscore must be escaped before Markdown rendering"
    )
    # Intentional Markdown formatting MUST still be present
    assert "*AS\\_ROID*" in text, "bold formatting around symbol preserved"
    assert "[chart](" in text, "chart link preserved"


def test_velocity_alert_url_path_not_escaped():
    """Site #6 (no-escape pin): coin_id sits inside a URL path; escaping it
    would break the link target. This test PINS the no-escape decision so a
    future 'helpful' PR that escapes coin_id is caught.
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
    # URL path must contain the bare underscore — escaped form breaks CoinGecko
    assert "(https://www.coingecko.com/en/coins/asteroid_coin)" in text, (
        "coin_id in URL path must NOT be escaped (URL paths use literal _)"
    )
    assert "asteroid\\_coin" not in text, (
        "coin_id in URL path must NOT be escaped"
    )
```

- [ ] **Step 7.2: Run test to verify it fails**

Run: `uv run pytest tests/test_parse_mode_hygiene.py::test_velocity_alert_escapes_user_data_fields -v`
Expected: FAIL — symbol/name not escaped.

- [ ] **Step 7.3: Apply fix**

Edit `scout/velocity/detector.py` lines 159-177 (`format_velocity_alert`). At top of the function, import `_escape_md`:

```python
def format_velocity_alert(detections: list[dict]) -> str:
    """Render a Markdown Telegram message for the given detections.

    Caller may pass raw dict fields; this function applies _escape_md to
    every user-data field interpolated into Markdown formatters (symbol,
    name). URL path fields (coin_id) are NOT escaped because Telegram
    requires literal characters inside [label](url) link targets. See
    CLAUDE.md §12b for the parse-mode hygiene rule.
    """
    from scout.alerter import _escape_md

    lines: list[str] = ["*Velocity Alerts* (1h pump)"]
    for det in detections:
        ch_1h = det.get("price_change_1h") or 0.0
        ch_24h = det.get("price_change_24h")
        mcap = det.get("market_cap")
        vol = det.get("volume_24h")
        ratio = det.get("vol_mcap_ratio") or 0.0
        price = det.get("current_price")
        ch_24h_s = f"{ch_24h:+.1f}%" if ch_24h is not None else "?"
        url = f"https://www.coingecko.com/en/coins/{det['coin_id']}"
        symbol_safe = _escape_md(det.get("symbol", ""))
        name_safe = _escape_md(det.get("name", ""))
        lines.append(
            f"\n*{symbol_safe}* — {name_safe}\n"
            f"1h: *{ch_1h:+.1f}%* | 24h: {ch_24h_s} | price: {_fmt_price(price)}\n"
            f"mcap: {_fmt_usd(mcap)} | vol: {_fmt_usd(vol)} | v/mc: {ratio:.2f}\n"
            f"[chart]({url})"
        )
    return "\n".join(lines)
```

- [ ] **Step 7.4: Run test to verify it passes**

Run: `uv run pytest tests/test_parse_mode_hygiene.py -v`
Expected: PASS.

- [ ] **Step 7.5: Verify no existing velocity tests regressed**

Run: `uv run pytest tests/ -k velocity -v`
Expected: All existing velocity tests pass.

- [ ] **Step 7.6: Commit**

```bash
git add scout/velocity/detector.py tests/test_parse_mode_hygiene.py
git commit -m "fix(velocity): escape symbol+name with _escape_md, preserve Markdown (BL-NEW-PARSE-MODE-AUDIT site #6)"
```

---

## Task 8: Fix site #7 — primary send_alert path (audit-missed)

**Files:**
- Modify: `scout/alerter.py:15-72` (`format_alert_message`)
- Modify: `tests/test_alerter.py:46` (update existing assertion to expect escaped form)
- Test: `tests/test_parse_mode_hygiene.py`

This site was missed by the audit. Plan-review reviewer A discovered it during scope/coverage review. It's the **primary candidate-alert path** (every gate-pass alert from `scout/main.py:871`). Body is intentionally Markdown-formatted (`*{token_name}*` bold wrapper). Fix shape mirrors velocity (#6): keep Markdown, escape interpolated user-data fields.

- [ ] **Step 8.1: Write failing test**

Append to `tests/test_parse_mode_hygiene.py`:

```python
def test_format_alert_message_escapes_user_data_fields(token_factory):
    """Site #7: format_alert_message at scout/alerter.py:15 must escape
    user-data fields (token_name, ticker, chain, virality_class, signal
    names, mirofish_report) so Telegram's MarkdownV1 parser does not
    consume underscores.

    Signal names always contain underscores (gainers_early, hard_loss, etc.);
    this site has been mangling every candidate alert since the codebase's
    inception.
    """
    from scout.alerter import format_alert_message

    token = token_factory(
        contract_address="0xabc_def",  # NOTE underscored — URL must NOT escape
        chain="solana_test",  # NOTE underscored — must be escaped (body field)
        token_name="AS_ROID",
        ticker="AS_RD",
        market_cap_usd=75000,
        quant_score=80,
        narrative_score=75,
        conviction_score=78,
        virality_class="High_Test",  # NOTE underscored
        mirofish_report="Has under_score chars",
    )
    signals = ["vol_liq_ratio", "momentum_ratio"]
    msg = format_alert_message(token, signals)

    # User-data fields appear in escaped form
    assert r"AS\_ROID" in msg, "token_name underscore must be escaped"
    assert r"AS\_RD" in msg, "ticker underscore must be escaped"
    assert r"solana\_test" in msg, "chain underscore must be escaped"
    assert r"High\_Test" in msg, "virality_class underscore must be escaped"
    assert r"vol\_liq\_ratio" in msg, "signal name underscore must be escaped"
    assert r"momentum\_ratio" in msg, "signal name underscore must be escaped"
    assert r"under\_score" in msg, "mirofish_report underscore must be escaped"
    # The intentional *bold* wrapping around token_name MUST still be present
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
```

- [ ] **Step 8.2: Run test to verify it fails**

Run: `uv run pytest tests/test_parse_mode_hygiene.py::test_format_alert_message_escapes_user_data_fields -v`
Expected: FAIL — fields not yet escaped.

- [ ] **Step 8.3: Update existing alerter test that asserts raw signal name**

Edit `tests/test_alerter.py:46`. Change:

```python
    assert "vol_liq_ratio" in msg
```

to:

```python
    assert r"vol\_liq\_ratio" in msg
```

This existing test ran fine pre-fix (signal name interpolated raw); post-fix it must assert the escaped form.

- [ ] **Step 8.4: Apply fix in format_alert_message**

Edit `scout/alerter.py:15-72`. Apply `_escape_md` to each user-data interpolation. Final form of the function:

```python
def format_alert_message(token: CandidateToken, signals: list[str]) -> str:
    """Format a candidate token into a human-readable alert message.

    Caller may pass raw model fields; this function applies _escape_md to
    every user-data field interpolated into Markdown formatters (token_name,
    ticker, chain, virality_class, signal names, mirofish_report). URL path
    fields (contract_address) are NOT escaped because Telegram requires
    literal characters inside [label](url) link targets. Sent with
    parse_mode='Markdown' (see send_alert at line 189). See CLAUDE.md §12b
    for the parse-mode hygiene rule.
    """
    lines: list[str] = []

    lines.append("⚠️ WARNING: RESEARCH ONLY - Not financial advice")
    lines.append("")
    lines.append(
        f"*{_escape_md(token.token_name)}* "
        f"({_escape_md(token.ticker)}) — {_escape_md(token.chain)}"
    )
    lines.append(f"Market Cap: ${token.market_cap_usd:,.0f}")
    lines.append("")

    # Conviction breakdown
    conviction_display = (
        f"{token.conviction_score:.1f}" if token.conviction_score is not None else "N/A"
    )
    quant_display = str(token.quant_score) if token.quant_score is not None else "N/A"
    narrative_display = (
        str(token.narrative_score) if token.narrative_score is not None else "N/A"
    )

    lines.append(f"Conviction Score: {conviction_display}")
    lines.append(f"  Quant: {quant_display}")
    if token.narrative_score is not None:
        lines.append(f"  Narrative: {narrative_display}")

    # Signals — each signal_type contains underscores; escape per-element
    lines.append("")
    lines.append("Signals: " + ", ".join(_escape_md(s) for s in signals))

    # Virality
    if token.virality_class is not None:
        lines.append(f"Virality: {_escape_md(token.virality_class)}")

    # Narrative summary — LLM-generated, can contain any markdown chars
    if token.mirofish_report is not None:
        lines.append(f"Narrative: {_escape_md(token.mirofish_report)}")

    # CoinGecko signal flags
    cg_flags = []
    if "momentum_ratio" in signals:
        cg_flags.append("Momentum: 1h gain accelerating vs 24h")
    if "vol_acceleration" in signals:
        cg_flags.append("Volume Spike: current vol >> 7d average")
    if "cg_trending_rank" in signals:
        cg_flags.append(f"CG Trending: rank #{token.cg_trending_rank or '?'}")
    if cg_flags:
        lines.append("")
        lines.append("CoinGecko Signals:")
        for flag in cg_flags:
            lines.append(f"  {flag}")

    # Source link — CoinGecko tokens use CG URL, others use DEXScreener.
    # contract_address is escaped because it appears in a URL path; while
    # URLs typically don't have markdown chars, hex addresses can include
    # underscores in some chains.
    lines.append("")
    if token.chain == "coingecko":
        lines.append(f"https://www.coingecko.com/en/coins/{token.contract_address}")
    else:
        lines.append(f"https://dexscreener.com/{token.chain}/{token.contract_address}")

    return "\n".join(lines)
```

Note: `send_alert` at line 189-220 keeps its hardcoded `"parse_mode": "Markdown"` at line 209 unchanged. The fix is purely on the body, not the parse_mode.

- [ ] **Step 8.5: Run new + existing alerter tests**

Run: `uv run pytest tests/test_parse_mode_hygiene.py tests/test_alerter.py -v`
Expected: PASS.

- [ ] **Step 8.6: Commit**

```bash
git add scout/alerter.py tests/test_alerter.py tests/test_parse_mode_hygiene.py
git commit -m "fix(alerter): escape user-data fields in format_alert_message (BL-NEW-PARSE-MODE-AUDIT site #7, plan-review discovery)"
```

---

## Task 8.5: Integration tests (wire-level — both primitives)

**Files:**
- Test: `tests/test_parse_mode_hygiene.py`

Per design-review reviewer C: source-level pins (Layer 2) miss wire-level regressions. Add one `aioresponses`-mocked test per primitive to assert the actual POST payload to `api.telegram.org/.../sendMessage` carries the right `parse_mode` field and body content.

- [ ] **Step 8.5.1: Write parse_mode=None integration test**

Append to `tests/test_parse_mode_hygiene.py`:

```python
@pytest.mark.asyncio
async def test_dispatch_with_parse_mode_none_omits_parse_mode_from_payload(
    settings_factory,
):
    """Wire-level: when parse_mode=None is passed to send_telegram_message,
    the JSON payload posted to Telegram does NOT include a parse_mode field
    (per scout/alerter.py:143-144). This is the wire-level pin behind the
    Layer 2 source-pins for sites #1-5.
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
async def test_dispatch_with_parse_mode_markdown_sends_escape_friendly_payload(
    settings_factory,
):
    """Wire-level: when parse_mode='Markdown' is passed and the caller has
    already _escape_md-ed user-data fields, the payload carries the escaped
    form AND parse_mode=Markdown. This is the wire-level pin for sites #6, #7.
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
            await send_telegram_message(
                f"*{_escape_md('AS_ROID')}* alert",
                session,
                settings,
                # default parse_mode="Markdown"
            )

    assert captured_payload["parse_mode"] == "Markdown"
    assert "AS\\_ROID" in captured_payload["text"], (
        "user-data field must be wire-level escaped"
    )
    assert "*AS\\_ROID*" in captured_payload["text"], (
        "intentional Markdown bold must reach the wire"
    )
```

- [ ] **Step 8.5.2: Run integration tests**

Run: `uv run pytest tests/test_parse_mode_hygiene.py -v -k integration_or_dispatch`
Expected: both new tests PASS.

- [ ] **Step 8.5.3: Commit**

```bash
git add tests/test_parse_mode_hygiene.py
git commit -m "test(parse-mode): wire-level integration tests for both primitives (design-review fold)"
```

---

## Task 9: Final-pass full test suite + lint

- [ ] **Step 9.1: Run full test suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests pass, no regressions introduced.

- [ ] **Step 9.2: Format**

Run: `uv run black scout/ tests/`
Expected: No-op or only formatting touch on the modified files.

- [ ] **Step 9.3: If formatter changed anything, commit**

```bash
git add -u
git commit -m "style: black formatting after parse-mode-hygiene fixes" || echo "nothing to commit"
```

---

## Task 10: Update lessons + carry-forward

- [ ] **Step 10.1: Append entry to `tasks/lessons.md`**

Add at end of `tasks/lessons.md`:

```markdown
## 2026-05-13 — Parse-mode hygiene Class-3 fixes (BL-NEW-PARSE-MODE-AUDIT)

**Lesson:** Default `parse_mode="Markdown"` in `scout.alerter.send_telegram_message` is a footgun for any caller whose body interpolates user-data fields with underscores. The trending_catch auto-suspend bug (§2.9, fixed in PR #106) was not unique — 6 other HIGH ACTUAL sites had been silently emitting mangled alerts for the codebase's lifetime.

**Rule (extension of CLAUDE.md §12b parse_mode addendum):** Any call to `send_telegram_message` whose body interpolates `signal_type`, `symbol`, `ticker`, LLM-generated text, or any other field that may contain `_ * [ ] \`` must either:
1. Pass `parse_mode=None` (preferred for system-health / digest / plain-text alerts), OR
2. Wrap each user-data field with `_escape_md()` AND keep `parse_mode="Markdown"` (only when the message intentionally uses Markdown formatters like `*bold*` or `[link](url)`).

**Default:** when in doubt, choose `parse_mode=None`. Markdown is only worth keeping when the formatting is operator-visible and load-bearing (e.g., velocity alerts with clickable chart links).
```

- [ ] **Step 10.2: Commit**

```bash
git add tasks/lessons.md
git commit -m "docs(lessons): parse-mode hygiene Class-3 rule (CLAUDE.md §12b extension)"
```

---

## Out-of-scope follow-ups (filed for separate PRs)

1. **HIGH POTENTIAL re-evaluation (3 sites in `scout/main.py`):** observe production logs for 7 days post-deploy; if any mangled alerts appear, fix those sites in a follow-up PR. Plan-review reviewer A flagged `scout/main.py:434` (counter-arg) as a HIGH ACTUAL candidate; the underlying ticker+LLM-output Markdown-special-char risk is real but matches the audit's HIGH POTENTIAL classification — deferred per audit policy. (Reviewer A's bracket-as-link-anchor reasoning was incorrect: Telegram MarkdownV1 requires `[label](url)` adjacency, not bare `[HIGH]`.)
2. **`scout/narrative/digest.py:format_daily_digest`:** plan review confirmed zero callers in `scout/`. Dead-path. Carry-forward: delete if it stays unused at next sweep, or wire-and-fix if a caller reappears.
3. **Audit-methodology gap (THIS PR's discovery):** the audit grepped `send_telegram_message` only and missed `send_alert` at `scout/alerter.py:189` — a separate function with its own hardcoded `parse_mode=Markdown` `session.post` call. Carry-forward: a follow-up audit should grep ALL `parse_mode` occurrences in `scout/` (not just `send_telegram_message` callers) AND ALL direct `session.post(...sendMessage...)` patterns. Filed for the next audit cycle.
4. **CLAUDE.md §12b scope expansion:** the rule currently scopes parse_mode discipline to auto-suspend-style write-time alerts; the audit (and this PR) demonstrate that the same Markdown-mangling failure mode applies to ALL Telegram-send paths with user-data interpolation, including `send_alert`. Promote in the dedicated rule-promotion session.

## Rollback plan

Each of the 6 fixes is a single-line change adding a kwarg (or, for velocity, three lines adding `_escape_md`). Rollback is `git revert <commit>` per site, no cascading dependencies. The branch is structured so each task is a single self-contained commit — partial rollback is supported.

## Deploy plan

After PR merges to master:
1. `ssh root@srilu-vps 'cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} +'` (per `feedback_clear_pycache_on_deploy.md`)
2. `ssh root@srilu-vps 'systemctl restart gecko-scout gecko-narrative gecko-trading-engine'` (whichever services touch the 6 fixed paths)
3. Wait for next alert fire on each of the 6 paths (calibration: weekly Mon; weekly_digest: weekly Mon; narrative_heating: continuous; paper_digest: daily; secondwave: continuous; velocity: continuous). Verify rendering is clean in Telegram.
4. If any service fails to start, `git revert` and redeploy.
