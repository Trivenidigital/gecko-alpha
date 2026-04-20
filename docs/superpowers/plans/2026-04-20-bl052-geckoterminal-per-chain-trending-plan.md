# BL-052 — GeckoTerminal Per-Chain Trending Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `gt_trending` scoring signal (+15 pts) that fires when a candidate token appears at rank ≤ `GT_TRENDING_TOP_N` (default 10) in a chain's GeckoTerminal `trending_pools` response.

**Architecture:** Capture `idx+1` as `gt_trending_rank` in the existing `fetch_trending_pools` parse loop → preserve through `aggregate()` via `_PRESERVE_FIELDS` → award points in `scorer.score`. No new HTTP calls. No DB changes. Mirrors `cg_trending_rank` pattern precisely. Bumps `SCORER_MAX_RAW` by +15 (base value read from current master — `183` without BL-051, `203` with BL-051 merged).

**Tech Stack:** Python 3.11+ asyncio, Pydantic v2, pytest-asyncio auto mode, aioresponses, structlog.

**Branch:** `feat/bl-052-geckoterminal-per-chain-trending` (already cut from master).

**Spec reference:** `docs/superpowers/specs/2026-04-20-bl052-geckoterminal-per-chain-trending-design.md`

**Master-state note for the implementer (read this before Task 1):** Before starting Task 1, run:
```bash
grep -n "import structlog\|logger = structlog" scout/scorer.py
grep -n "^SCORER_MAX_RAW" scout/scorer.py
```
If `scorer.py` already has the structlog logger lines (BL-051 merged) → skip the "add structlog import" bullet in Task 6 and target `SCORER_MAX_RAW = current_value + 15`. If it does NOT have them (BL-051 not yet merged — current state as of this plan) → add them, and target `SCORER_MAX_RAW = 183 + 15 = 198`. In either case, the pin test asserts the computed target.

---

### Task 1: Add `gt_trending_rank` field to `CandidateToken`

**Files:**
- Modify: `scout/models.py` (add one field next to `cg_trending_rank`)
- Create: `tests/test_models_gt_trending_rank.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_models_gt_trending_rank.py`:
```python
"""Pin the gt_trending_rank CandidateToken field (BL-052)."""

from scout.models import CandidateToken


def test_gt_trending_rank_defaults_to_none():
    token = CandidateToken(
        contract_address="0xabc",
        chain="base",
        token_name="Test",
        ticker="TST",
    )
    assert token.gt_trending_rank is None


def test_gt_trending_rank_accepts_int():
    token = CandidateToken(
        contract_address="0xabc",
        chain="base",
        token_name="Test",
        ticker="TST",
        gt_trending_rank=3,
    )
    assert token.gt_trending_rank == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models_gt_trending_rank.py -v`
Expected: FAIL — `gt_trending_rank` field does not exist on `CandidateToken`.

- [ ] **Step 3: Add the field to `scout/models.py`**

Find the `cg_trending_rank: int | None = None` line in `CandidateToken`. Directly after it, add:

```python
    # Populated by GeckoTerminal trending_pools parser (BL-052).
    # 1-based rank within the emitting chain's trending_pools list
    # (position 1 = most-traded). None if the token was not sourced from
    # GT trending or the rank info was unavailable.
    gt_trending_rank: int | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models_gt_trending_rank.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run full suite to confirm no regressions**

Run: `uv run pytest --tb=short -q 2>&1 | tail -10`
Expected: all previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add scout/models.py tests/test_models_gt_trending_rank.py
git commit -m "feat(bl-052): add gt_trending_rank field to CandidateToken"
```

---

### Task 2: Add `GT_TRENDING_TOP_N` setting

**Files:**
- Modify: `scout/config.py`
- Modify: `.env.example`
- Create: `tests/test_config_gt_trending.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_gt_trending.py`:
```python
"""Pin the GT_TRENDING_TOP_N default (BL-052)."""

import pytest

from scout.config import Settings


@pytest.fixture
def base_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")


def test_gt_trending_top_n_default(base_env):
    s = Settings()
    assert s.GT_TRENDING_TOP_N == 10


def test_gt_trending_top_n_override(base_env, monkeypatch):
    monkeypatch.setenv("GT_TRENDING_TOP_N", "3")
    s = Settings()
    assert s.GT_TRENDING_TOP_N == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_gt_trending.py -v`
Expected: FAIL — `GT_TRENDING_TOP_N` attribute does not exist.

- [ ] **Step 3: Add setting to `scout/config.py`**

In `scout/config.py`, find the "-------- Second-Wave Detection --------" section or any logical grouping with the existing data-source knobs. Add a new block ABOVE the paper-trading section (approximately after the Trending Snapshot Tracker block around line 140):

```python
    # -------- GeckoTerminal Per-Chain Trending (BL-052) --------
    GT_TRENDING_TOP_N: int = 10
```

- [ ] **Step 4: Update `.env.example`**

In `.env.example`, find the `# === Paper Trading Engine ===` header (last section) and INSERT a new section directly above it:

```bash

# === GeckoTerminal Per-Chain Trending (BL-052) ===
# Rank threshold for the gt_trending scoring signal (+15 pts).
# Lower = stricter; default 10 = top half of GT's ~20-per-chain list.
GT_TRENDING_TOP_N=10
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_config_gt_trending.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add scout/config.py .env.example tests/test_config_gt_trending.py
git commit -m "feat(bl-052): add GT_TRENDING_TOP_N setting (default 10)"
```

---

### Task 3: Capture rank in `fetch_trending_pools`

**Files:**
- Modify: `scout/ingestion/geckoterminal.py`
- Create: `tests/test_geckoterminal_rank.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_geckoterminal_rank.py`:
```python
"""Test gt_trending_rank capture in fetch_trending_pools (BL-052)."""

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.ingestion.geckoterminal import fetch_trending_pools


def _pool(addr, name="TestPool / SOL", fdv=100_000.0, liq=20_000.0, vol=80_000.0):
    return {
        "attributes": {
            "name": name,
            "fdv_usd": fdv,
            "reserve_in_usd": liq,
            "volume_usd": {"h24": vol},
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_{addr}"}}
        },
    }


@pytest.fixture
def settings():
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        CHAINS=["solana"],
    )


async def test_fetch_trending_pools_assigns_rank_by_index(settings):
    pools = [_pool("addr1"), _pool("addr2"), _pool("addr3")]
    with aioresponses() as m:
        m.get(
            "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
            payload={"data": pools},
        )
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    ranks = [t.gt_trending_rank for t in tokens]
    addrs = [t.contract_address for t in tokens]
    assert ranks == [1, 2, 3]
    assert addrs == ["addr1", "addr2", "addr3"]


async def test_fetch_trending_pools_empty_data_emits_nothing(settings):
    with aioresponses() as m:
        m.get(
            "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
            payload={"data": []},
        )
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)
    assert tokens == []


async def test_fetch_trending_pools_skips_malformed_but_preserves_rank_order(settings):
    # idx 0 = valid, idx 1 = malformed (missing relationships), idx 2 = valid
    pools = [
        _pool("good1"),
        {"attributes": {}, "relationships": {}},  # will throw when parsed
        _pool("good3"),
    ]
    with aioresponses() as m:
        m.get(
            "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
            payload={"data": pools},
        )
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    # Rank 2 is "burned" (idx 1 failed); ranks stay positional, not compacted.
    ranks = [t.gt_trending_rank for t in tokens]
    addrs = [t.contract_address for t in tokens]
    assert addrs == ["good1", "good3"]
    assert ranks == [1, 3]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_geckoterminal_rank.py -v`
Expected: FAIL — tokens have `gt_trending_rank=None` (rank capture not yet implemented).

- [ ] **Step 3: Implement rank capture in `scout/ingestion/geckoterminal.py`**

Replace the inner `for pool in data.get("data", []):` loop (current lines 37-48) with:

```python
        # NB: GT returns trending_pools in rank order; idx 0 = most-traded.
        for idx, pool in enumerate(data.get("data", [])):
            try:
                token = CandidateToken.from_geckoterminal(pool, chain=chain)
                token = token.model_copy(update={"gt_trending_rank": idx + 1})
                if (
                    settings.MIN_MARKET_CAP
                    <= token.market_cap_usd
                    <= settings.MAX_MARKET_CAP
                ):
                    candidates.append(token)
            except Exception as e:
                logger.warning("Failed to parse GeckoTerminal pool", error=str(e))
                continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_geckoterminal_rank.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run full suite to confirm no regressions**

Run: `uv run pytest --tb=short -q 2>&1 | tail -10`
Expected: all tests pass. Any existing geckoterminal tests that asserted `gt_trending_rank is None` must have been absent (new field defaults to None, unchanged for all non-GT tokens).

- [ ] **Step 6: Commit**

```bash
git add scout/ingestion/geckoterminal.py tests/test_geckoterminal_rank.py
git commit -m "feat(bl-052): capture gt_trending_rank in fetch_trending_pools"
```

---

### Task 4: Preserve `gt_trending_rank` through aggregator

**Files:**
- Modify: `scout/aggregator.py`
- Create: `tests/test_aggregator_gt_rank.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_aggregator_gt_rank.py`:
```python
"""Test gt_trending_rank preservation through aggregate() (BL-052)."""

from scout.aggregator import aggregate
from scout.models import CandidateToken


def _tok(addr, rank=None, source_chain="solana"):
    return CandidateToken(
        contract_address=addr,
        chain=source_chain,
        token_name="Test",
        ticker="TST",
        gt_trending_rank=rank,
    )


def test_gt_trending_rank_preserved_when_gt_arrives_first():
    # GT first (rank=3), then DexScreener (rank=None)
    result = aggregate([_tok("0xabc", rank=3), _tok("0xabc", rank=None)])
    assert len(result) == 1
    assert result[0].gt_trending_rank == 3


def test_gt_trending_rank_preserved_when_dex_arrives_first():
    # DexScreener first (rank=None), then GT (rank=3)
    result = aggregate([_tok("0xabc", rank=None), _tok("0xabc", rank=3)])
    assert len(result) == 1
    assert result[0].gt_trending_rank == 3


def test_gt_trending_rank_unchanged_for_non_duplicate():
    result = aggregate([_tok("0xabc", rank=5)])
    assert len(result) == 1
    assert result[0].gt_trending_rank == 5


def test_gt_trending_rank_none_stays_none_for_non_duplicate():
    result = aggregate([_tok("0xabc", rank=None)])
    assert len(result) == 1
    assert result[0].gt_trending_rank is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_aggregator_gt_rank.py -v`
Expected: `test_gt_trending_rank_preserved_when_gt_arrives_first` FAILS (rank=3 clobbered to None). The other three pass by coincidence.

- [ ] **Step 3: Update `scout/aggregator.py`**

In the `_PRESERVE_FIELDS` list, add `gt_trending_rank` just below `cg_trending_rank`, and add a contract-hardening comment above the list:

Replace:
```python
# Fields to preserve from earlier entries if the later entry has None
_PRESERVE_FIELDS = [
    "cg_trending_rank",
    "price_change_1h",
    "price_change_24h",
    "vol_7d_avg",
    "txns_h1_buys",
    "txns_h1_sells",
]
```

With:
```python
# Preserve first non-None value on merge.
# Changing this semantics breaks all rank and enrichment signals.
_PRESERVE_FIELDS = [
    "cg_trending_rank",
    "gt_trending_rank",
    "price_change_1h",
    "price_change_24h",
    "vol_7d_avg",
    "txns_h1_buys",
    "txns_h1_sells",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_aggregator_gt_rank.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run full suite**

Run: `uv run pytest --tb=short -q 2>&1 | tail -10`
Expected: no regressions. Existing `cg_trending_rank` aggregator tests should continue passing.

- [ ] **Step 6: Commit**

```bash
git add scout/aggregator.py tests/test_aggregator_gt_rank.py
git commit -m "feat(bl-052): preserve gt_trending_rank through aggregator"
```

---

### Task 5: Bump `SCORER_MAX_RAW` (pin test first)

**Files:**
- Modify: `scout/scorer.py` (constant + docstring)
- Create: `tests/test_scorer_max_raw_bumped_gt.py`

- [ ] **Step 1: Determine target value**

Run: `grep -n "^SCORER_MAX_RAW" scout/scorer.py`
- If output shows `183` → target is `198`.
- If output shows `203` (BL-051 merged) → target is `218`.
- Any other value → STOP, do not guess; report to the controller.

Let `TARGET = current + 15`. Use this literal integer in Step 2 and Step 4 below (substitute wherever the placeholder `TARGET_VALUE` appears).

- [ ] **Step 2: Write the failing pin test**

Create `tests/test_scorer_max_raw_bumped_gt.py`:
```python
"""Pin SCORER_MAX_RAW after BL-052 velocity (gt_trending +15)."""

from scout import scorer


def test_scorer_max_raw_bumped_for_gt_trending():
    assert scorer.SCORER_MAX_RAW == TARGET_VALUE  # replace with 198 or 218
```

Remember to replace `TARGET_VALUE` with the actual literal.

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_scorer_max_raw_bumped_gt.py -v`
Expected: FAIL — current value is either 183 or 203, not TARGET_VALUE.

- [ ] **Step 4: Bump the constant and update the scorer docstring**

In `scout/scorer.py`:
- Change `SCORER_MAX_RAW = <current>` to `SCORER_MAX_RAW = <TARGET_VALUE>`.
- In the module docstring, find the `Max raw:` line (around line 24). Update the arithmetic to include `+15`. Example (if current base was 183 and adding gt_trending):
  - BEFORE: `Max raw: 30+8+25+15+15+15+20+25+15+5+10 = 183 points`
  - AFTER: `Max raw: 30+8+25+15+15+15+20+25+15+15+5+10 = 198 points`
- In the docstring signal list (the "CoinGecko signals:" or similar grouping), add a bullet: `- gt_trending (rank <= GT_TRENDING_TOP_N): 15 points -- GT per-chain DEX trending (BL-052)`. Place it directly below `cg_trending_rank`.

- [ ] **Step 5: Run pin test (expect some existing cap-at-100 tests to now fail)**

Run: `uv run pytest tests/test_scorer_max_raw_bumped_gt.py -v`
Expected: the new pin test PASSES, but other scorer tests that assert a raw-score cap of 100 may now fail (expected; handled in Task 6 sub-step).

Run: `uv run pytest tests/ -k scorer --tb=short -q 2>&1 | tail -30`
Note any tests that fail with "assert 85 == 100" or similar normalization-related assertions. Do NOT commit until Task 6 lands.

- [ ] **Step 6: Commit (bundle with Task 6)**

**Do NOT commit yet — bundle with Task 6 so the cap-at-100 tests remain green at every commit boundary.**

---

### Task 6: Add `gt_trending` signal + structlog event

**Files:**
- Modify: `scout/scorer.py` (add signal block, add structlog import if missing)
- Create: `tests/test_scorer_gt_trending.py`
- Possibly modify: any existing scorer tests whose setup assumed raw=MAX_PRE_BUMP to still cap at 100

- [ ] **Step 1: Check whether structlog is already imported in scorer.py**

Run: `grep -n "import structlog\|logger = structlog" scout/scorer.py`
- If BOTH present → skip Step 3a's import addition.
- If NEITHER present → perform Step 3a.
- If only one present → report to controller; this is a corrupted state.

- [ ] **Step 2: Write failing tests**

Create `tests/test_scorer_gt_trending.py`:
```python
"""Test gt_trending scoring signal (BL-052)."""

import pytest
import structlog
from structlog.testing import capture_logs

from scout.config import Settings
from scout.models import CandidateToken
from scout.scorer import score


@pytest.fixture
def settings():
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )


def _tok(**overrides):
    defaults = dict(
        contract_address="0xabc",
        chain="base",
        token_name="Test",
        ticker="TST",
        market_cap_usd=50_000.0,
        liquidity_usd=20_000.0,
        volume_24h_usd=10_000.0,
        holder_count=50,
        holder_growth_1h=0,
    )
    defaults.update(overrides)
    return CandidateToken(**defaults)


def test_gt_trending_fires_at_rank_1(settings):
    token = _tok(gt_trending_rank=1)
    _, signals = score(token, settings)
    assert "gt_trending" in signals


def test_gt_trending_does_not_fire_at_rank_11_default_top_n_10(settings):
    token = _tok(gt_trending_rank=11)
    _, signals = score(token, settings)
    assert "gt_trending" not in signals


def test_gt_trending_skipped_when_rank_none(settings):
    token = _tok(gt_trending_rank=None)
    _, signals = score(token, settings)
    assert "gt_trending" not in signals


def test_gt_trending_boundary_top_n_3(settings):
    strict = settings.model_copy(update={"GT_TRENDING_TOP_N": 3})
    assert "gt_trending" in score(_tok(gt_trending_rank=3), strict)[1]
    assert "gt_trending" not in score(_tok(gt_trending_rank=4), strict)[1]


def test_gt_trending_fires_logs_event(settings):
    token = _tok(gt_trending_rank=2, ticker="ROCKET", contract_address="0xdead")
    with capture_logs() as logs:
        score(token, settings)
    events = [e for e in logs if e.get("event") == "gt_trending_signal_fired"]
    assert len(events) == 1
    e = events[0]
    assert e["token"] == "ROCKET"
    assert e["contract_address"] == "0xdead"
    assert e["chain"] == "base"
    assert e["gt_trending_rank"] == 2


def test_gt_trending_silent_below_threshold_no_log(settings):
    token = _tok(gt_trending_rank=99)
    with capture_logs() as logs:
        score(token, settings)
    events = [e for e in logs if e.get("event") == "gt_trending_signal_fired"]
    assert events == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_scorer_gt_trending.py -v`
Expected: FAIL — signal not yet implemented.

- [ ] **Step 3a (conditional): Add structlog import to `scout/scorer.py`**

If Step 1 confirmed structlog is absent, add at the top of `scout/scorer.py` (after existing imports):
```python
import structlog

logger = structlog.get_logger(__name__)
```

If present already (BL-051 merged), skip.

- [ ] **Step 4: Insert the new signal block**

In `scout/scorer.py`, find the current "Signal 9: CG trending rank -- 15 points" block (lines 142-145). Directly after it (and BEFORE Signal 10 `solana_bonus`), insert:

```python
    # Signal 10: GeckoTerminal per-chain trending rank -- 15 points (BL-052)
    if (
        token.gt_trending_rank is not None
        and token.gt_trending_rank <= settings.GT_TRENDING_TOP_N
    ):
        points += 15
        signals.append("gt_trending")
        logger.info(
            "gt_trending_signal_fired",
            token=token.ticker,
            contract_address=token.contract_address,
            chain=token.chain,
            gt_trending_rank=token.gt_trending_rank,
        )
```

Renumber the trailing comment lines:
- `# Signal 10: Solana chain bonus` → `# Signal 11: Solana chain bonus`
- `# Signal 11: Score velocity bonus` → `# Signal 12: Score velocity bonus`

(If BL-051 has merged, the signal numbering on master will already include `velocity_boost` as Signal 10 — in that case, insert the gt_trending block at a coherent position and renumber accordingly. Aim for: ...cg_trending → velocity_boost → gt_trending → solana_bonus → score_velocity. Use your judgment; the comment numbers are documentation only and have no runtime effect.)

- [ ] **Step 5: Run gt_trending tests**

Run: `uv run pytest tests/test_scorer_gt_trending.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Address any "cap-at-100" regressions from Task 5's bump**

Run: `uv run pytest tests/ -k scorer --tb=short -q 2>&1 | tail -30`
Look for tests asserting a specific normalized score against a fixture that previously yielded `raw == MAX_PRE_BUMP → normalized = 100`. These tests now see `raw == MAX_PRE_BUMP → normalized = ~92`.

**Fix strategy:** For each such failing test, add `gt_trending_rank=1` (or any ≤10) to the fixture setup. This pushes raw from `MAX_PRE_BUMP` to `MAX_PRE_BUMP+15 = TARGET`, restoring the cap-at-100 behavior.

This is the same pattern used in BL-051 Task 6b (check `git log feat/bl-051-dexscreener-boosts-poster --oneline` if it exists for precedent). If no tests fail, skip this step.

- [ ] **Step 7: Run full suite**

Run: `uv run pytest --tb=short -q 2>&1 | tail -10`
Expected: all tests pass.

- [ ] **Step 8: Format**

Run: `uv run black scout/ tests/`
Stage any reformatted files.

- [ ] **Step 9: Commit Task 5 + Task 6 together**

```bash
git add scout/scorer.py tests/test_scorer_max_raw_bumped_gt.py tests/test_scorer_gt_trending.py <any_fixture_files_updated>
git commit -m "feat(bl-052): add gt_trending scoring signal (+15 pts) and bump SCORER_MAX_RAW"
```

---

### Task 7: Integration test — full `run_cycle` pipeline

**Files:**
- Create: `tests/test_main_pipeline_gt_trending.py`

- [ ] **Step 1: Read the existing main pipeline test for patterns**

Read `tests/test_main.py` (or similar integration-test file in the repo) to understand the existing pattern for mocking `run_cycle` via aioresponses. Match the fixture/setup style used there.

- [ ] **Step 2: Write the integration test**

Create `tests/test_main_pipeline_gt_trending.py` modeled on the existing pattern. Skeleton:

```python
"""Integration test: gt_trending signal propagates through run_cycle (BL-052)."""

import pytest
from aioresponses import aioresponses
# ... match imports from tests/test_main.py

# Mock fixtures at minimum:
#   - GT /networks/solana/trending_pools returns 1 pool with contract 0xtarget
#   - DexScreener search returns same contract 0xtarget (minimal fields)
#   - CoinGecko /coins/markets returns []
#   - /search/trending returns []
#   - GoPlus / DexScreener /token-boosts/top: return whatever the default harness expects
#
# Call run_cycle with dry_run=True.
# Assert: the emitted candidate for 0xtarget has "gt_trending" in signals_fired.
#
# If existing tests patch fetch_top_boosts / fetch_X directly, do the same here;
# otherwise stick to aioresponses HTTP-layer mocking.
```

Look at `tests/test_main_pipeline_top_boosts.py` (added by BL-051) if it exists on any branch — it is the nearest-sibling test and the cleanest template. If the BL-051 branch is still un-merged, checkout-and-diff is acceptable for reference (do not copy, use the pattern only).

- [ ] **Step 3: Run the integration test**

Run: `uv run pytest tests/test_main_pipeline_gt_trending.py -v`
Expected: PASS.

If the test cannot be written without modifying `run_cycle`'s signature, STOP and escalate to the controller. The spec forbids touching `run_cycle`.

- [ ] **Step 4: Run full suite**

Run: `uv run pytest --tb=short -q 2>&1 | tail -10`
Expected: all tests pass.

- [ ] **Step 5: Format**

Run: `uv run black scout/ tests/`

- [ ] **Step 6: Commit**

```bash
git add tests/test_main_pipeline_gt_trending.py
git commit -m "test(bl-052): integration test for gt_trending in run_cycle"
```

---

### Task 8: Final verification + push

- [ ] **Step 1: Full test run with verbose summary**

Run: `uv run pytest --tb=short -q 2>&1 | tail -15`
Expected: all previously-passing tests plus all new tests green. Record the final pass count.

- [ ] **Step 2: Dry-run the pipeline locally**

Run: `uv run python -m scout.main --dry-run --cycles 1 2>&1 | tail -40`
Expected: no exceptions. Look for either `gt_trending_signal_fired` log entry (good — real data produced a match) OR complete silence on that event (acceptable — live data didn't produce a trending match this cycle). Look for zero NEW error-level logs vs. baseline.

- [ ] **Step 3: Verify `.env.example` formatting**

Run: `git diff master -- .env.example | head -30`
Confirm the GT section is a standalone block with its own header, placed before the Paper Trading block (not appended inside it).

- [ ] **Step 4: Push**

```bash
git push -u origin feat/bl-052-geckoterminal-per-chain-trending
```

- [ ] **Step 5: Report**

Report to controller:
- Final test count (baseline N → final M).
- Commit SHAs in branch since master (`git log master..HEAD --oneline`).
- Whether `gt_trending_signal_fired` fired during the live dry-run.
- Any cap-at-100 tests that needed fixture updates in Task 6 (list them).
- Whether BL-051 was merged before this branch (Y/N) and the resulting `SCORER_MAX_RAW` target (198 or 218).

---

## Post-implementation (controller task, not implementer)

- Dispatch parallel spec-compliance + code-quality reviewers for the full branch diff.
- Address any blocking findings.
- Create PR targeting `master` with the standard template + link to spec and plan.
- Dispatch parallel PR reviewers.
- Do NOT merge. Do NOT deploy.
