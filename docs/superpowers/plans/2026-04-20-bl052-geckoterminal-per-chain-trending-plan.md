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
def settings(settings_factory):
    # Use the shared settings_factory fixture from tests/conftest.py for
    # consistency with tests/test_geckoterminal.py. This avoids hand-rolling
    # required kwargs and keeps the test's config surface aligned with sibling
    # tests.
    return settings_factory(
        CHAINS=["solana"],
        MIN_MARKET_CAP=10_000,
        MAX_MARKET_CAP=500_000,
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
    # idx 0 = valid, idx 1 = malformed (fdv_usd raises ValueError on float()), idx 2 = valid.
    # NB: a truly empty {"attributes": {}, "relationships": {}} does NOT raise in
    # from_geckoterminal (it produces contract_address="" + mcap=0 which is then
    # filtered by the mcap floor, NOT the except path). Using a non-numeric fdv
    # triggers the intended exception path.
    pools = [
        _pool("good1"),
        {"attributes": {"fdv_usd": "KABOOM"}, "relationships": {}},
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

- [ ] **Step 3b: Update the aggregator observability log**

The existing `aggregator.py:44` line counts only `cg_trending_rank`. Update it to also count `gt_trending_rank` so the log reflects both sources:

Replace:
```python
    # Log how many tokens have trending rank after aggregation
    ranked = sum(1 for t in seen.values() if t.cg_trending_rank is not None)
    if ranked > 0:
        logger.info("aggregator_trending_preserved", ranked_tokens=ranked)
```

With:
```python
    # Log how many tokens have any trending rank (CG or GT) after aggregation
    cg_ranked = sum(1 for t in seen.values() if t.cg_trending_rank is not None)
    gt_ranked = sum(1 for t in seen.values() if t.gt_trending_rank is not None)
    if cg_ranked > 0 or gt_ranked > 0:
        logger.info(
            "aggregator_trending_preserved",
            cg_ranked=cg_ranked,
            gt_ranked=gt_ranked,
        )
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

Create `tests/test_scorer_max_raw_bumped_gt.py`. Pick ONE of the two variants below based on Step 1's reading:

**Variant A — `SCORER_MAX_RAW` on master is currently 183 (BL-051 NOT merged) → TARGET=198:**
```python
"""Pin SCORER_MAX_RAW after BL-052 gt_trending signal (+15)."""

from scout import scorer


def test_scorer_max_raw_bumped_for_gt_trending():
    assert scorer.SCORER_MAX_RAW == 198
```

**Variant B — `SCORER_MAX_RAW` on master is currently 203 (BL-051 already merged) → TARGET=218:**
```python
"""Pin SCORER_MAX_RAW after BL-052 gt_trending signal (+15)."""

from scout import scorer


def test_scorer_max_raw_bumped_for_gt_trending():
    assert scorer.SCORER_MAX_RAW == 218
```

Do NOT leave a placeholder token in the committed file — the integer literal must be present. If unsure which variant applies, re-run `grep -n "^SCORER_MAX_RAW" scout/scorer.py` and pick accordingly.

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

Run: `grep -n "^import structlog\|^logger = structlog" scout/scorer.py`
- Count distinct lines matched. Desired end-state: exactly one `import structlog` line AND exactly one `logger = structlog.get_logger(__name__)` line at module scope.
- If both are already present → skip Step 3a.
- If either or both are missing → perform Step 3a, adding ONLY the missing line(s). Do not duplicate.
- Idempotency wins over gatekeeping — ensure the end state matches, regardless of starting state.

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

- [ ] **Step 6: Update `/183` literal references in existing tests (MANDATORY, not optional)**

`tests/test_scorer.py` contains 12 hardcoded `183` references (exact-normalized-integer assertions and comments). After bumping `SCORER_MAX_RAW` from 183 to 198 (Variant A) or 203 to 218 (Variant B), ALL of these break.

**Exact list (line numbers from current master; run `grep -n "183" tests/test_scorer.py` if unsure):**
- L49: comment `int(30*100/183)=16` → update divisor.
- L143: comment `int(25*100/183)=13` → update divisor.
- L172: comment `int(15*100/183)=8` → update divisor.
- L188: comment `int(8*100/183)=4` → update divisor.
- L204: comment `int(5*100/183)=2` → update divisor.
- L367: comment `int(33*100/183) = 18, 3 signals -> *1.15 = int(20.7) = 20` → update divisor; recompute the integer values.
- L439: comment `int(78*100/183)=42, *1.15=int(48.3)=48` → update divisor; recompute.
- L455: comment `int(30*100/183)=16` → update divisor.
- L542: comment `int(15*100/183)=8` → update divisor.
- L927: comment `5 pts raw -> normalized=int(5*100/183)=2` → update divisor.
- L1030: comment `30+8+25+15+15+15+20+25+15+5+10 = 183` → update to include `+15` and new sum.
- **L1031: assertion `assert SCORER_MAX_RAW == 183` → UPDATE the literal to the new target (198 or 218).** This was a pin test; after the bump, it must assert the new value. This overlaps with the new pin test in `test_scorer_max_raw_bumped_gt.py` — that is fine, two pin tests of the same invariant are acceptable.
- L1034: docstring `int(30*100/183)=16` → update.
- L1046: assertion `assert points == int(30 * 100 / 183)` → update the literal divisor.

**Method — for each affected line, change the literal divisor from `183` to the target:**
- Variant A (target=198): replace `183` → `198`. Recompute integer comments (e.g. `int(30*100/198)=15`, not 16).
- Variant B (target=218): replace `183` → `218`. Recompute integer comments (e.g. `int(30*100/218)=13`).

For L367, L439 specifically: both the first integer AND the multiplied integer in the trailing comment need recomputation.

**Automation hint:** Use `sed -i 's/\b183\b/198/g' tests/test_scorer.py` (or `218` for Variant B) as a FIRST pass, then manually re-verify each comment's computed integer matches the new math. Do NOT commit until the full suite passes.

If additional test files (other than `test_scorer.py`) also hardcode `183` as a divisor, update them with the same process: `grep -rn "100 */ *183\|/ *183" tests/` before proceeding.

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

Read `tests/test_main.py` to understand the existing pattern for exercising `run_cycle`. Look at how it:
- Instantiates `Settings` (likely via `settings_factory` from conftest).
- Opens / closes DB and `aiohttp.ClientSession`.
- Patches or mocks the ingestion layer.

`tests/test_main.py` is the sole reference — the BL-051 sibling file (`test_main_pipeline_top_boosts.py`) is NOT on master. Do not fetch from other branches; derive the pattern from master alone.

- [ ] **Step 2: Decide on mocking approach**

Two viable approaches (pick whichever test_main.py already uses):

**(a) `aioresponses` HTTP-layer mock** — intercepts `aiohttp.ClientSession.get/post` at the URL level. No signature change to `run_cycle`. Mock each URL the pipeline hits; extras return 404 by default (handled by existing error paths).

**(b) `monkeypatch.setattr` on the ingestion functions themselves** — simpler, less brittle, but requires knowing the function names (`scout.ingestion.geckoterminal.fetch_trending_pools`, etc.). Existing test_main.py is likely using this approach based on BL-051's pattern; verify before writing.

- [ ] **Step 3: Write the integration test**

Create `tests/test_main_pipeline_gt_trending.py`. Minimum assertions:
- One fake GT pool at rank 1 with contract `0xtarget` on chain `solana`, mcap in range.
- All other ingestion sources (DexScreener, CoinGecko markets, CoinGecko trending, DexScreener top-boosts if present) return empty lists / empty responses.
- After `run_cycle` (dry-run), the pipeline produces candidates. Locate the one for `0xtarget` and assert `"gt_trending" in candidate.signals_fired`.

Example skeleton (adapt to whatever mocking test_main.py uses):

```python
"""Integration test: gt_trending signal propagates through run_cycle (BL-052)."""

from unittest.mock import AsyncMock, patch

import pytest

from scout.models import CandidateToken
# plus whatever imports test_main.py uses for run_cycle


@pytest.mark.asyncio
async def test_gt_trending_signal_propagates_through_run_cycle(
    settings_factory, tmp_path, <other fixtures from test_main.py>
):
    trending_token = CandidateToken(
        contract_address="0xtarget",
        chain="solana",
        token_name="Target",
        ticker="TGT",
        market_cap_usd=50_000.0,
        liquidity_usd=20_000.0,
        volume_24h_usd=10_000.0,
        gt_trending_rank=1,
    )

    with patch(
        "scout.ingestion.geckoterminal.fetch_trending_pools",
        new=AsyncMock(return_value=[trending_token]),
    ), patch(
        "scout.ingestion.dexscreener.fetch_trending",
        new=AsyncMock(return_value=[]),
    ), patch(
        "scout.ingestion.coingecko.fetch_markets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "scout.ingestion.coingecko.fetch_trending",
        new=AsyncMock(return_value=[]),
    ):
        # call run_cycle here matching test_main.py's pattern
        # inspect DB or capture produced candidates
        ...

    # assert: the candidate for 0xtarget includes "gt_trending" in signals_fired
    ...
```

Replace the `...` blocks and patched names with whatever `test_main.py` uses. If `run_cycle` persists to DB, query the DB. If it returns a list, capture the return value. **Match the existing pattern — do not invent a new one.**

If the existing test_main.py patches additional sources not listed here (e.g. `fetch_top_boosts` from BL-051 if it has merged), add patches for those too (all returning empty) so our new source is the only positive signal.

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

Run: `uv run python -m scout.main --dry-run --cycles 1 2>&1 | tee /tmp/bl052_dryrun.log | tail -40`
Expected: no exceptions and zero NEW error-level logs vs. baseline.

Then verify the GT trending path actually executed by running these greps against the captured log:

```bash
# Primary success signal — a trending token cleared the scorer
grep -c "gt_trending_signal_fired" /tmp/bl052_dryrun.log || true

# Fallback evidence — the aggregator observed gt-ranked tokens even if none
# scored high enough for the signal to fire (stablecoin dust, mcap filter, etc.)
grep -E "aggregator_trending_preserved" /tmp/bl052_dryrun.log || true
```

Acceptance:
- If `gt_trending_signal_fired` appears ≥1 time → ✅ end-to-end path verified.
- Else if `aggregator_trending_preserved` shows `gt_ranked>0` → ✅ ingestion + aggregation verified (signal didn't fire this cycle because no trending token hit the mcap/liquidity gates, which is acceptable).
- If **neither** appears → ❌ something is wired wrong. Inspect the log for a GeckoTerminal fetch error or a silent exception in the trending loop before proceeding. Do not push.

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
