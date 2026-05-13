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

## Per-site fix matrix

| # | Site | Current state | Fix primitive | Why this primitive |
|---|---|---|---|---|
| 1 | `scout/narrative/agent.py:557` (call) + `scout/narrative/digest.py:format_heating_alert` (body) | default `parse_mode=Markdown`; body has NO `*`/`[`/`` ` `` formatters | `parse_mode=None` at call site | Body is plain text; no formatting intent to preserve |
| 2 | `scout/narrative/agent.py:715` (call) + `scout/trading/digest.py:build_paper_digest` (body) | default `parse_mode=Markdown`; body has NO formatters; body interpolates `best_symbol`, `worst_symbol`, AND per-signal `{sig}` (every signal_type has underscores) | `parse_mode=None` at call site | Body is plain text; signal_type interpolation mangles on every digest |
| 3 | `scout/secondwave/detector.py:285` (call) + `scout/secondwave/alerts.py:format_secondwave_alert` (body) | default `parse_mode=Markdown`; body has NO formatters; interpolates `token_name`, `ticker`, peak_signals join, reacc_signals join | `parse_mode=None` at call site | Body is plain text; 4 vulnerable fields per alert |
| 4 | `scout/trading/calibrate.py:354` (apply path) | default `parse_mode=Markdown`; body interpolates `d.signal_type` (always has `_`), `c.field`, `d.reason` inside `[…]` brackets | `parse_mode=None` at call site | Already-documented inconsistency: dry-run path uses `parse_mode=None` (per docstring at `calibrate.py:459` and `alerter.py:131-135`), apply path doesn't. Same `[reason]` shape → same fix. |
| 5 | `scout/trading/weekly_digest.py:335,340` (both call sites in same function) | default `parse_mode=Markdown`; body interpolates `e["symbol"]`, `sig` (signal_type), `r["combo_key"]`, plus `[…]` section markers | `parse_mode=None` at both call sites | System-health digest; brackets are section delimiters, not Markdown |
| 6 | `scout/velocity/detector.py:193` (call) + `scout/velocity/detector.py:159-177:format_velocity_alert` (body) | default `parse_mode=Markdown`; body INTENTIONALLY uses `*bold*` and `[chart](url)` link formatting; interpolates `det['symbol']`, `det['name']` raw | `_escape_md(det['symbol'])` + `_escape_md(det['name'])` in `format_velocity_alert`; keep `parse_mode=Markdown` | Author intended Markdown rendering (bold/clickable links); escape only the user-data fields |

## File structure

**Files to modify (production code):**
- `scout/narrative/agent.py` — add `parse_mode=None` at line 557
- `scout/narrative/agent.py` — add `parse_mode=None` at line 715
- `scout/secondwave/detector.py` — add `parse_mode=None` at line 285
- `scout/trading/calibrate.py` — add `parse_mode=None` at line 354
- `scout/trading/weekly_digest.py` — add `parse_mode=None` at lines 335 and 340
- `scout/velocity/detector.py` — wrap `det['symbol']` and `det['name']` with `_escape_md(...)` in `format_velocity_alert`

**Files to create (tests):**
- `tests/test_parse_mode_hygiene.py` — regression tests covering all 6 sites + a structural test that walks `scout/` for any future site missing the hygiene primitive.

**Files NOT to touch in this PR (out of scope):**
- `scout/main.py:350,433,1521` — 3 HIGH POTENTIAL sites. Per audit §"What's NOT in this audit" point 3, these need post-deploy log observation first; deferred to a separate follow-up PR after a 7-day soak.
- The 5 LOW/MEDIUM sites — body shape unlikely to mangle; per audit, no change needed.
- `scout/narrative/digest.py:format_daily_digest` — not in audit's HIGH ACTUAL list; review-only verification that no call site dispatches it via the vulnerable path. (Audit may have missed it; flagged in carry-forward.)

## Self-review notes (in advance)

1. **Audit field coverage is partial.** The audit lists "best_symbol, worst_symbol" for site #2, but `build_paper_digest` also interpolates `{sig}` (signal_type) per-line. `parse_mode=None` fixes both — no per-field analysis needed because the whole-body parse mode is disabled. ✓
2. **Velocity escape coverage.** `_escape_md` covers `\ _ * [ ] \``. Token symbols and names in practice contain underscores (`AS_ROID`) and occasionally brackets/asterisks. The url and other markdown intent stays. ✓
3. **Calibrate dry-run inconsistency.** The apply path at line 354 was overlooked when the dry-run path was fixed per silent-failure C1. This PR closes the gap. ✓

---

## Task 1: Add structural test that catches future regressions

**Files:**
- Create: `tests/test_parse_mode_hygiene.py`

This task creates the test scaffold and one safety-net structural test. Per-site tests follow in Tasks 2-7.

- [ ] **Step 1.1: Write the structural-coverage test**

Create `tests/test_parse_mode_hygiene.py`:

```python
"""Regression tests for BL-NEW-PARSE-MODE-AUDIT — Class-3 silent rendering corruption.

These tests pin the 6 HIGH ACTUAL sites confirmed in
tasks/findings_parse_mode_audit_2026_05_12.md against future regression.
Each per-site test sends a payload containing markdown-special characters
in the user-data fields (signal_type with underscores, symbol with
underscores) and asserts that the rendered Telegram payload either uses
parse_mode=None OR escapes the user-data field before interpolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------
# Helper: capture the payload that scout.alerter.send_telegram_message
# would post to Telegram.
# ---------------------------------------------------------------------


def _capture_send(monkeypatch):
    """Patch scout.alerter.send_telegram_message to capture call args.

    Returns a list that will be appended-to on each call. Each appended
    entry is a dict with keys: text, parse_mode.
    """
    captured: list[dict] = []

    async def fake_send(text, session, settings, *, parse_mode="Markdown"):
        captured.append({"text": text, "parse_mode": parse_mode})

    monkeypatch.setattr("scout.alerter.send_telegram_message", fake_send)
    return captured
```

- [ ] **Step 1.2: Run to verify it imports cleanly**

Run: `uv run pytest tests/test_parse_mode_hygiene.py -v`
Expected: `no tests collected` (file has no test functions yet) — but no import error.

- [ ] **Step 1.3: Commit**

```bash
git add tests/test_parse_mode_hygiene.py
git commit -m "test(parse-mode): scaffold regression test module for BL-NEW-PARSE-MODE-AUDIT"
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

- [ ] **Step 6.3: Apply fix at line 335**

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

- [ ] **Step 6.4: Apply fix at line 340**

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

- [ ] **Step 6.5: Run test to verify it passes**

Run: `uv run pytest tests/test_parse_mode_hygiene.py -v`
Expected: PASS.

- [ ] **Step 6.6: Commit**

```bash
git add scout/trading/weekly_digest.py tests/test_parse_mode_hygiene.py
git commit -m "fix(weekly_digest): both dispatches use parse_mode=None (BL-NEW-PARSE-MODE-AUDIT site #5)"
```

---

## Task 7: Fix site #6 — velocity alert (keep Markdown, escape user-data)

**Files:**
- Modify: `scout/velocity/detector.py:159-177` (`format_velocity_alert`)
- Test: `tests/test_parse_mode_hygiene.py`

Velocity is the ONLY site that intentionally uses Markdown (`*bold*` for emphasis, `[chart](url)` for clickable link). Fix is `_escape_md` on user-data fields, NOT `parse_mode=None`.

- [ ] **Step 7.1: Write failing test**

Append:

```python
def test_velocity_alert_escapes_user_data_fields():
    """Site #6: format_velocity_alert preserves *bold* + [chart](url) intent,
    but symbol/name are passed through _escape_md so underscores don't get
    consumed as italics markers.

    Verification: build an alert with a symbol containing '_' and assert
    that the rendered output contains the escaped form (backslash-underscore)
    NOT the bare underscore.
    """
    from scout.velocity.detector import format_velocity_alert

    detection = {
        "symbol": "AS_ROID",
        "name": "Asteroid_Test",
        "coin_id": "asteroid",
        "price_change_1h": 50.0,
        "price_change_24h": 30.0,
        "market_cap": 1_000_000.0,
        "volume_24h": 500_000.0,
        "vol_mcap_ratio": 0.5,
        "current_price": 0.0001,
    }
    text = format_velocity_alert([detection])
    # The user-data fields must appear ESCAPED (with backslash before _)
    # so Telegram Markdown parser does not consume the underscores.
    assert "AS\\_ROID" in text, (
        "symbol underscore must be escaped before Markdown rendering"
    )
    assert "Asteroid\\_Test" in text, (
        "name underscore must be escaped before Markdown rendering"
    )
    # And the intentional Markdown formatting MUST still be present
    assert "*AS\\_ROID*" in text, "bold formatting around symbol preserved"
    assert "[chart](" in text, "chart link preserved"
```

- [ ] **Step 7.2: Run test to verify it fails**

Run: `uv run pytest tests/test_parse_mode_hygiene.py::test_velocity_alert_escapes_user_data_fields -v`
Expected: FAIL — symbol/name not escaped.

- [ ] **Step 7.3: Apply fix**

Edit `scout/velocity/detector.py` lines 159-177 (`format_velocity_alert`). At top of the function, import `_escape_md`:

```python
def format_velocity_alert(detections: list[dict]) -> str:
    """Render a Markdown Telegram message for the given detections."""
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

## Task 8: Final-pass full test suite + lint

- [ ] **Step 8.1: Run full test suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests pass, no regressions introduced.

- [ ] **Step 8.2: Format**

Run: `uv run black scout/ tests/`
Expected: No-op or only formatting touch on the 6 files above.

- [ ] **Step 8.3: If formatter changed anything, commit**

```bash
git add -u
git commit -m "style: black formatting after parse-mode-hygiene fixes" || echo "nothing to commit"
```

---

## Task 9: Update lessons + carry-forward

- [ ] **Step 9.1: Append entry to `tasks/lessons.md`**

Add at end of `tasks/lessons.md`:

```markdown
## 2026-05-13 — Parse-mode hygiene Class-3 fixes (BL-NEW-PARSE-MODE-AUDIT)

**Lesson:** Default `parse_mode="Markdown"` in `scout.alerter.send_telegram_message` is a footgun for any caller whose body interpolates user-data fields with underscores. The trending_catch auto-suspend bug (§2.9, fixed in PR #106) was not unique — 6 other HIGH ACTUAL sites had been silently emitting mangled alerts for the codebase's lifetime.

**Rule (extension of CLAUDE.md §12b parse_mode addendum):** Any call to `send_telegram_message` whose body interpolates `signal_type`, `symbol`, `ticker`, LLM-generated text, or any other field that may contain `_ * [ ] \`` must either:
1. Pass `parse_mode=None` (preferred for system-health / digest / plain-text alerts), OR
2. Wrap each user-data field with `_escape_md()` AND keep `parse_mode="Markdown"` (only when the message intentionally uses Markdown formatters like `*bold*` or `[link](url)`).

**Default:** when in doubt, choose `parse_mode=None`. Markdown is only worth keeping when the formatting is operator-visible and load-bearing (e.g., velocity alerts with clickable chart links).
```

- [ ] **Step 9.2: Commit**

```bash
git add tasks/lessons.md
git commit -m "docs(lessons): parse-mode hygiene Class-3 rule (CLAUDE.md §12b extension)"
```

---

## Out-of-scope follow-ups (filed for separate PRs)

1. **HIGH POTENTIAL re-evaluation (3 sites in `scout/main.py`):** observe production logs for 7 days post-deploy; if any mangled alerts appear, fix those sites in a follow-up PR.
2. **`scout/narrative/digest.py:format_daily_digest`:** interpolates `r.get('symbol')` and `c.get('key')` raw. Not in audit's HIGH ACTUAL list; audit may have missed it. Worth a separate dispatcher-side review to confirm whether the daily-digest dispatch path was already fixed.
3. **CLAUDE.md §12b scope expansion:** the rule currently scopes parse_mode discipline to auto-suspend-style write-time alerts; the audit (and this PR) demonstrate that the same Markdown-mangling failure mode applies to all `send_telegram_message` call sites with user-data interpolation. Promote in the dedicated rule-promotion session.

## Rollback plan

Each of the 6 fixes is a single-line change adding a kwarg (or, for velocity, three lines adding `_escape_md`). Rollback is `git revert <commit>` per site, no cascading dependencies. The branch is structured so each task is a single self-contained commit — partial rollback is supported.

## Deploy plan

After PR merges to master:
1. `ssh root@srilu-vps 'cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} +'` (per `feedback_clear_pycache_on_deploy.md`)
2. `ssh root@srilu-vps 'systemctl restart gecko-scout gecko-narrative gecko-trading-engine'` (whichever services touch the 6 fixed paths)
3. Wait for next alert fire on each of the 6 paths (calibration: weekly Mon; weekly_digest: weekly Mon; narrative_heating: continuous; paper_digest: daily; secondwave: continuous; velocity: continuous). Verify rendering is clean in Telegram.
4. If any service fails to start, `git revert` and redeploy.
