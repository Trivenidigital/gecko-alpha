# BL-051 — DexScreener Top-Boosts Poller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a DexScreener `/token-boosts/top/v1` poller and `velocity_boost` scorer signal (+20 pts) that decorates existing pipeline candidates with cumulative paid-boost data.

**Architecture:** Decorator pattern — the new fetch runs as the 6th arg of the Stage-1 `asyncio.gather` and its output passes through a dedicated `apply_boost_decorations()` step in the aggregator that attaches `boost_total_amount` / `boost_rank` to already-deduped `CandidateToken`s. No new token population, no DB schema change, no MiroFish impact.

**Tech Stack:** Python 3.11+ async (`asyncio`/`aiohttp`), Pydantic v2, `structlog`, `pytest-asyncio` auto mode, `aioresponses` for HTTP mocking.

**Spec:** `docs/superpowers/specs/2026-04-19-bl051-dexscreener-top-boosts-design.md`

---

## File Structure

**Modify:**
- `scout/models.py` — add two optional fields to `CandidateToken`
- `scout/config.py` — add two settings in a new config block
- `.env.example` — document the new env vars
- `scout/ingestion/dexscreener.py` — add `BoostInfo`, helpers, poller, module-level cache
- `scout/aggregator.py` — add `apply_boost_decorations()` function
- `scout/scorer.py` — insert new Signal 10 `velocity_boost`; bump `SCORER_MAX_RAW` 183 → 203
- `scout/main.py` — extend Stage-1 `asyncio.gather` + add decorator call site
- `tests/test_scorer.py` — update comments/golden values shifted by normalization change

**Create-in-existing-file:**
- `tests/test_dexscreener.py` — new tests for `fetch_top_boosts`
- `tests/test_aggregator.py` — new tests for `apply_boost_decorations`
- `tests/test_scorer.py` — new tests for `velocity_boost` signal
- `tests/test_main_pipeline_top_boosts.py` — new integration test (new file)

---

### Task 1: Add boost fields to `CandidateToken` model

**Why first:** every downstream component references these fields. The model must compile before any other task can import.

**Files:**
- Modify: `scout/models.py:43-44` (insert new fields after `cg_trending_rank`)
- Test: `tests/test_models_boost_fields.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_models_boost_fields.py`:

```python
"""Tests for BL-051 boost fields on CandidateToken."""

from scout.models import CandidateToken


def test_candidate_token_boost_fields_default_to_none():
    t = CandidateToken(
        contract_address="0xabc",
        chain="solana",
        token_name="T",
        ticker="T",
    )
    assert t.boost_total_amount is None
    assert t.boost_rank is None


def test_candidate_token_boost_fields_accept_values():
    t = CandidateToken(
        contract_address="0xabc",
        chain="solana",
        token_name="T",
        ticker="T",
        boost_total_amount=1500.0,
        boost_rank=1,
    )
    assert t.boost_total_amount == 1500.0
    assert t.boost_rank == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models_boost_fields.py -v`
Expected: FAIL — Pydantic rejects the unknown keyword args (`boost_total_amount`, `boost_rank`).

- [ ] **Step 3: Implement the fields**

In `scout/models.py`, directly after the line `cg_trending_rank: int | None = None` (currently line 43), insert:

```python
    # Populated by DexScreener top-boosts decorator (BL-051)
    boost_total_amount: float | None = None
    boost_rank: int | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models_boost_fields.py -v`
Expected: PASS (both tests green).

- [ ] **Step 5: Commit**

```bash
git add scout/models.py tests/test_models_boost_fields.py
git commit -m "feat(bl-051): add boost_total_amount and boost_rank fields to CandidateToken"
```

---

### Task 2: Add config settings + `.env.example` entries

**Files:**
- Modify: `scout/config.py` (append a new block)
- Modify: `.env.example`
- Test: `tests/test_config_boosts.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_boosts.py`:

```python
"""Tests for BL-051 config settings."""

from scout.config import Settings


def test_min_boost_total_amount_default(settings_factory):
    s = settings_factory()
    assert s.MIN_BOOST_TOTAL_AMOUNT == 500.0


def test_dexscreener_top_boosts_poll_every_cycles_default(settings_factory):
    s = settings_factory()
    assert s.DEXSCREENER_TOP_BOOSTS_POLL_EVERY_CYCLES == 1


def test_min_boost_total_amount_override(settings_factory):
    s = settings_factory(MIN_BOOST_TOTAL_AMOUNT=1000.0)
    assert s.MIN_BOOST_TOTAL_AMOUNT == 1000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_boosts.py -v`
Expected: FAIL — `AttributeError` on `MIN_BOOST_TOTAL_AMOUNT`.

- [ ] **Step 3: Implement the settings**

In `scout/config.py`, after the `CoinGecko` block (around line 42, right before the `# MiroFish` block), append:

```python
    # -------- DexScreener Top Boosts (BL-051) --------
    DEXSCREENER_TOP_BOOSTS_POLL_EVERY_CYCLES: int = 1
    MIN_BOOST_TOTAL_AMOUNT: float = 500.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_boosts.py -v`
Expected: PASS.

- [ ] **Step 5: Update `.env.example`**

In `.env.example`, append (location: end of file, or after existing `MIN_VOL_ACCEL_RATIO` if present):

```
# -------- DexScreener Top Boosts (BL-051) --------
DEXSCREENER_TOP_BOOSTS_POLL_EVERY_CYCLES=1
MIN_BOOST_TOTAL_AMOUNT=500
```

- [ ] **Step 6: Commit**

```bash
git add scout/config.py tests/test_config_boosts.py .env.example
git commit -m "feat(bl-051): add MIN_BOOST_TOTAL_AMOUNT and poll-cycles settings"
```

---

### Task 3: Add `BoostInfo` dataclass + chain/address normalization helpers

**Files:**
- Modify: `scout/ingestion/dexscreener.py` (add to top of module, after existing constants/imports)
- Test: `tests/test_dexscreener_normalize.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_dexscreener_normalize.py`:

```python
"""Tests for BL-051 normalization helpers in dexscreener.py."""

import pytest

from scout.ingestion.dexscreener import (
    BoostInfo,
    _normalize_chain_id,
    _normalize_address,
)


def test_boost_info_is_frozen():
    b = BoostInfo(chain="solana", address="ABC", total_amount=1500.0)
    with pytest.raises(Exception):
        b.chain = "base"  # frozen dataclass


def test_boost_info_fields():
    b = BoostInfo(chain="solana", address="ABC", total_amount=1500.0)
    assert b.chain == "solana"
    assert b.address == "ABC"
    assert b.total_amount == 1500.0


def test_normalize_chain_id_known():
    assert _normalize_chain_id("solana") == "solana"
    assert _normalize_chain_id("base") == "base"
    assert _normalize_chain_id("ethereum") == "ethereum"


def test_normalize_chain_id_unknown_passes_through_lower():
    assert _normalize_chain_id("SomeChain") == "somechain"


def test_normalize_address_evm_lowercases():
    assert _normalize_address("ethereum", "0xAbC123") == "0xabc123"
    assert _normalize_address("base", "0xDEADBEEF") == "0xdeadbeef"


def test_normalize_address_solana_preserves_case():
    solana_addr = "7GAGFk8aJMbNSRtCh8bB9x6eVpKZwxzMnB3UsNYukgmo"
    assert _normalize_address("solana", solana_addr) == solana_addr


def test_normalize_address_sui_preserves_case():
    sui_addr = "0xABCDef"
    assert _normalize_address("sui", sui_addr) == sui_addr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dexscreener_normalize.py -v`
Expected: FAIL — `ImportError` on `BoostInfo`, `_normalize_chain_id`, `_normalize_address`.

- [ ] **Step 3: Implement the helpers**

In `scout/ingestion/dexscreener.py`, right after the existing `import` block (after line 10) and the `logger = structlog.get_logger()` line (line 12), add:

```python
from dataclasses import dataclass
```

Then after the existing constants (after line 19 — after `REQUEST_TIMEOUT = ...`), add:

```python
TOP_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"

# Last-raw top-boosts payload, kept for optional future dashboard surfacing.
# Not consumed by the pipeline. Parity with `last_raw_markets` in coingecko.py.
last_raw_top_boosts: list[dict] = []


@dataclass(frozen=True, slots=True)
class BoostInfo:
    """Lightweight internal container for one top-boost entry.

    Not persisted, not serialized. Kept in memory between fetch and
    `apply_boost_decorations` in aggregator.py.
    """

    chain: str
    address: str
    total_amount: float


_CHAIN_ID_MAP = {
    "solana": "solana",
    "base": "base",
    "ethereum": "ethereum",
    "arbitrum": "arbitrum",
    "bsc": "bsc",
    "polygon": "polygon",
    "avalanche": "avalanche",
    "optimism": "optimism",
    "fantom": "fantom",
}

# EVM-family chains where addresses are case-insensitive hex. All other
# chains (solana, sui, aptos, tron, ...) keep their native case.
_EVM_CHAINS = frozenset(
    {"ethereum", "base", "arbitrum", "bsc", "polygon", "avalanche", "optimism", "fantom"}
)


def _normalize_chain_id(chain_id: str) -> str:
    """Map DexScreener chainId to our internal chain slug.

    Unknown chainIds are lower-cased and passed through; the aggregator
    join will simply fail to match a candidate, which is the correct no-op.
    """
    key = (chain_id or "").lower()
    return _CHAIN_ID_MAP.get(key, key)


def _normalize_address(chain: str, address: str) -> str:
    """Normalize an address for join comparison.

    EVM chains: lower-case (EIP-55 checksum must match canonical lower form).
    Non-EVM chains (solana/sui/aptos/tron): preserve case — base58 and
    similar encodings are case-sensitive.
    """
    if chain in _EVM_CHAINS:
        return address.lower()
    return address
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_dexscreener_normalize.py -v`
Expected: PASS (all 7 tests green).

- [ ] **Step 5: Commit**

```bash
git add scout/ingestion/dexscreener.py tests/test_dexscreener_normalize.py
git commit -m "feat(bl-051): add BoostInfo dataclass and chain/address normalizers"
```

---

### Task 4: Implement `fetch_top_boosts`

**Files:**
- Modify: `scout/ingestion/dexscreener.py` (append the new async function after `fetch_trending`)
- Test: `tests/test_dexscreener.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dexscreener.py` (the file already has `mock_aiohttp` fixture and imports):

```python
from scout.ingestion.dexscreener import (
    fetch_top_boosts,
    TOP_BOOSTS_URL,
    BoostInfo,
)
from scout.ingestion import dexscreener as _dex_module


async def test_fetch_top_boosts_happy_path(mock_aiohttp, settings_factory):
    mock_aiohttp.get(
        TOP_BOOSTS_URL,
        payload=[
            {"chainId": "solana", "tokenAddress": "SOL_ADDR_1", "totalAmount": 1500},
            {"chainId": "base", "tokenAddress": "0xBASE1", "totalAmount": 900},
            {"chainId": "ethereum", "tokenAddress": "0xETH1", "totalAmount": 600},
        ],
    )
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        boosts = await fetch_top_boosts(session, settings)

    assert len(boosts) == 3
    assert boosts[0] == BoostInfo(chain="solana", address="SOL_ADDR_1", total_amount=1500.0)
    assert boosts[1] == BoostInfo(chain="base", address="0xBASE1", total_amount=900.0)
    assert boosts[2] == BoostInfo(chain="ethereum", address="0xETH1", total_amount=600.0)


async def test_fetch_top_boosts_empty_response(mock_aiohttp, settings_factory):
    mock_aiohttp.get(TOP_BOOSTS_URL, payload=[])
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        boosts = await fetch_top_boosts(session, settings)
    assert boosts == []


async def test_fetch_top_boosts_skips_missing_total_amount(mock_aiohttp, settings_factory):
    mock_aiohttp.get(
        TOP_BOOSTS_URL,
        payload=[
            {"chainId": "solana", "tokenAddress": "OK1", "totalAmount": 1000},
            {"chainId": "solana", "tokenAddress": "NO_TOTAL"},  # missing
            {"chainId": "solana", "tokenAddress": "BAD", "totalAmount": "oops"},  # not numeric
            {"chainId": "solana", "tokenAddress": "OK2", "totalAmount": 250},
        ],
    )
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        boosts = await fetch_top_boosts(session, settings)
    # Only the two valid entries pass.
    assert [b.address for b in boosts] == ["OK1", "OK2"]


async def test_fetch_top_boosts_skips_missing_chain_or_address(mock_aiohttp, settings_factory):
    mock_aiohttp.get(
        TOP_BOOSTS_URL,
        payload=[
            {"chainId": "solana", "tokenAddress": "", "totalAmount": 1000},  # empty addr
            {"chainId": "", "tokenAddress": "OK", "totalAmount": 1000},  # empty chain
            {"chainId": "solana", "tokenAddress": "GOOD", "totalAmount": 1000},
        ],
    )
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        boosts = await fetch_top_boosts(session, settings)
    assert [b.address for b in boosts] == ["GOOD"]


async def test_fetch_top_boosts_upstream_error_returns_empty(mock_aiohttp, settings_factory):
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        boosts = await fetch_top_boosts(session, settings)
    assert boosts == []


async def test_fetch_top_boosts_populates_module_cache(mock_aiohttp, settings_factory):
    _dex_module.last_raw_top_boosts.clear()
    mock_aiohttp.get(
        TOP_BOOSTS_URL,
        payload=[{"chainId": "solana", "tokenAddress": "X", "totalAmount": 100}],
    )
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        await fetch_top_boosts(session, settings)
    assert len(_dex_module.last_raw_top_boosts) == 1
    assert _dex_module.last_raw_top_boosts[0]["tokenAddress"] == "X"


async def test_fetch_top_boosts_cache_preserved_on_failure(mock_aiohttp, settings_factory):
    _dex_module.last_raw_top_boosts.clear()
    _dex_module.last_raw_top_boosts.append({"tokenAddress": "STALE", "totalAmount": 1})
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        boosts = await fetch_top_boosts(session, settings)
    assert boosts == []
    # Stale cache is preferred over empty.
    assert _dex_module.last_raw_top_boosts == [{"tokenAddress": "STALE", "totalAmount": 1}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dexscreener.py -v -k "top_boosts"`
Expected: FAIL — `ImportError` on `fetch_top_boosts`.

- [ ] **Step 3: Implement `fetch_top_boosts`**

Append to `scout/ingestion/dexscreener.py` (after the existing `fetch_trending` function):

```python
async def fetch_top_boosts(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[BoostInfo]:
    """Fetch cumulative top-boosted tokens from DexScreener.

    Returns a list of BoostInfo entries, ordered by the API's
    own `totalAmount` desc ranking (rank = index + 1 downstream).
    Never raises; upstream failures or schema drift yield an empty list.
    Populates `last_raw_top_boosts` on success; leaves it untouched on
    failure (stale-preferred-over-empty semantics).
    """
    raw = await _get_json(session, TOP_BOOSTS_URL)
    if not raw or not isinstance(raw, list):
        return []

    last_raw_top_boosts.clear()
    last_raw_top_boosts.extend(raw)

    results: list[BoostInfo] = []
    warned = False
    for entry in raw:
        chain_id = entry.get("chainId", "")
        address = entry.get("tokenAddress", "")
        total = entry.get("totalAmount")
        if not chain_id or not address or total is None:
            continue
        try:
            total_f = float(total)
        except (TypeError, ValueError):
            if not warned:
                logger.warning(
                    "top_boosts_bad_total_amount",
                    entry=entry,
                )
                warned = True
            continue
        chain = _normalize_chain_id(chain_id)
        results.append(BoostInfo(chain=chain, address=address, total_amount=total_f))

    logger.info(
        "dex_top_boosts_fetched",
        count=len(results),
        top_amount=results[0].total_amount if results else 0.0,
    )
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dexscreener.py -v -k "top_boosts"`
Expected: PASS (all 7 new tests green). Existing `test_fetch_trending_*` remain green.

- [ ] **Step 5: Commit**

```bash
git add scout/ingestion/dexscreener.py tests/test_dexscreener.py
git commit -m "feat(bl-051): implement fetch_top_boosts poller"
```

---

### Task 5: Implement `apply_boost_decorations` in aggregator

**Files:**
- Modify: `scout/aggregator.py`
- Test: `tests/test_aggregator.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_aggregator.py`:

```python
from scout.aggregator import apply_boost_decorations
from scout.ingestion.dexscreener import BoostInfo


EVM_ADDR_UPPER = "0xAbC0000000000000000000000000000000000001"
EVM_ADDR_LOWER = EVM_ADDR_UPPER.lower()
SOL_ADDR_A = "7GAGFk8aJMbNSRtCh8bB9x6eVpKZwxzMnB3UsNYukgmo"
SOL_ADDR_B = "7GAGFk8aJMbNSRtCh8bB9x6eVpKZwxzMnB3UsNYukgMO"  # differs in last two chars' case


def test_apply_boost_decorations_match_evm_case_insensitive(token_factory):
    cand = token_factory(contract_address=EVM_ADDR_UPPER, chain="ethereum")
    boost = BoostInfo(chain="ethereum", address=EVM_ADDR_LOWER, total_amount=1500.0)
    result = apply_boost_decorations([cand], [boost])
    assert len(result) == 1
    assert result[0].boost_total_amount == 1500.0
    assert result[0].boost_rank == 1


def test_apply_boost_decorations_match_reverse_case(token_factory):
    cand = token_factory(contract_address=EVM_ADDR_LOWER, chain="base")
    boost = BoostInfo(chain="base", address=EVM_ADDR_UPPER, total_amount=900.0)
    result = apply_boost_decorations([cand], [boost])
    assert result[0].boost_total_amount == 900.0
    assert result[0].boost_rank == 1


def test_apply_boost_decorations_solana_case_sensitive(token_factory):
    cand = token_factory(contract_address=SOL_ADDR_A, chain="solana")
    # A Solana boost whose address differs only by case MUST NOT match.
    boost = BoostInfo(chain="solana", address=SOL_ADDR_B, total_amount=1500.0)
    result = apply_boost_decorations([cand], [boost])
    assert result[0].boost_total_amount is None
    assert result[0].boost_rank is None


def test_apply_boost_decorations_solana_exact_match(token_factory):
    cand = token_factory(contract_address=SOL_ADDR_A, chain="solana")
    boost = BoostInfo(chain="solana", address=SOL_ADDR_A, total_amount=1500.0)
    result = apply_boost_decorations([cand], [boost])
    assert result[0].boost_total_amount == 1500.0


def test_apply_boost_decorations_no_match_leaves_candidate(token_factory):
    cand = token_factory(contract_address="0xY", chain="ethereum")
    boost = BoostInfo(chain="ethereum", address="0xX", total_amount=1500.0)
    result = apply_boost_decorations([cand], [boost])
    assert result[0].boost_total_amount is None
    assert result[0].boost_rank is None


def test_apply_boost_decorations_rank_order(token_factory):
    a = token_factory(contract_address="0xaaa1" + "0" * 36, chain="ethereum")
    b = token_factory(contract_address="0xbbb2" + "0" * 36, chain="ethereum")
    c = token_factory(contract_address="0xccc3" + "0" * 36, chain="ethereum")
    boosts = [
        BoostInfo(chain="ethereum", address="0xbbb2" + "0" * 36, total_amount=3000.0),
        BoostInfo(chain="ethereum", address="0xaaa1" + "0" * 36, total_amount=2000.0),
        BoostInfo(chain="ethereum", address="0xccc3" + "0" * 36, total_amount=1000.0),
    ]
    result = apply_boost_decorations([a, b, c], boosts)
    by_addr = {t.contract_address: t for t in result}
    assert by_addr["0xaaa1" + "0" * 36].boost_rank == 2
    assert by_addr["0xbbb2" + "0" * 36].boost_rank == 1
    assert by_addr["0xccc3" + "0" * 36].boost_rank == 3


def test_apply_boost_decorations_chain_must_match(token_factory):
    """Same address on two chains: only the matching-chain candidate is decorated."""
    cand_sol = token_factory(contract_address="SAME_ADDR_XYZ", chain="solana")
    cand_base = token_factory(contract_address="SAME_ADDR_XYZ", chain="base")
    boost = BoostInfo(chain="solana", address="SAME_ADDR_XYZ", total_amount=777.0)
    result = apply_boost_decorations([cand_sol, cand_base], [boost])
    by_chain = {t.chain: t for t in result}
    assert by_chain["solana"].boost_total_amount == 777.0
    assert by_chain["base"].boost_total_amount is None


def test_apply_boost_decorations_empty_inputs(token_factory):
    assert apply_boost_decorations([], []) == []
    cand = token_factory()
    assert apply_boost_decorations([cand], []) == [cand]
    assert apply_boost_decorations([], [BoostInfo("solana", "x", 100.0)]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_aggregator.py -v -k "boost"`
Expected: FAIL — `ImportError` on `apply_boost_decorations`.

- [ ] **Step 3: Implement `apply_boost_decorations`**

Append to `scout/aggregator.py`:

```python
from scout.ingestion.dexscreener import BoostInfo, _normalize_address


def apply_boost_decorations(
    candidates: list[CandidateToken],
    boosts: list[BoostInfo],
) -> list[CandidateToken]:
    """Decorate deduped candidates with DexScreener top-boost data (BL-051).

    Rank is derived positionally from the incoming `boosts` list order
    (index+1 = rank), reflecting the API's own totalAmount-desc ordering.
    Join key is (chain, normalized_address); EVM addresses are matched
    case-insensitive, non-EVM chains preserve case.

    Unmatched boost entries are silently dropped; unmatched candidates are
    returned unchanged (their `boost_total_amount` / `boost_rank` remain None).
    """
    if not boosts:
        return candidates

    boost_map: dict[tuple[str, str], tuple[float, int]] = {}
    for idx, b in enumerate(boosts):
        key = (b.chain, _normalize_address(b.chain, b.address))
        boost_map[key] = (b.total_amount, idx + 1)

    result: list[CandidateToken] = []
    for cand in candidates:
        key = (cand.chain, _normalize_address(cand.chain, cand.contract_address))
        hit = boost_map.get(key)
        if hit is None:
            result.append(cand)
            continue
        total, rank = hit
        result.append(
            cand.model_copy(update={"boost_total_amount": total, "boost_rank": rank})
        )
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_aggregator.py -v`
Expected: PASS — all new tests green AND existing aggregator tests still green.

- [ ] **Step 5: Commit**

```bash
git add scout/aggregator.py tests/test_aggregator.py
git commit -m "feat(bl-051): add apply_boost_decorations merge step"
```

---

### Task 6: Add `velocity_boost` signal + bump `SCORER_MAX_RAW`

**Files:**
- Modify: `scout/scorer.py` (docstring lines 3-25, constant line 32, signal insertion around line 145, header comment at line 147)
- Modify: `tests/test_scorer.py` (update golden values + constant assertion)
- Add: `tests/test_scorer_velocity_boost.py` (new — keeps the new tests isolated from the legacy test refactor)

- [ ] **Step 1: Write the failing tests for the new signal**

Create `tests/test_scorer_velocity_boost.py`:

```python
"""Tests for BL-051 velocity_boost signal."""

from scout.scorer import score, SCORER_MAX_RAW


def test_scorer_max_raw_is_203():
    # 30+8+25+15+15+15+20+25+15+20+5+10 = 203
    assert SCORER_MAX_RAW == 203


def test_velocity_boost_fires_above_threshold(token_factory, settings_factory):
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)
    token = token_factory(
        liquidity_usd=20_000,  # above MIN_LIQUIDITY_USD default
        boost_total_amount=1500.0,
        boost_rank=1,
    )
    points, signals = score(token, settings)
    assert "velocity_boost" in signals


def test_velocity_boost_silent_below_threshold(token_factory, settings_factory):
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)
    token = token_factory(
        liquidity_usd=20_000,
        boost_total_amount=100.0,
    )
    points, signals = score(token, settings)
    assert "velocity_boost" not in signals


def test_velocity_boost_silent_when_none(token_factory, settings_factory):
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)
    token = token_factory(
        liquidity_usd=20_000,
        boost_total_amount=None,
    )
    points, signals = score(token, settings)
    assert "velocity_boost" not in signals


def test_velocity_boost_at_threshold_fires(token_factory, settings_factory):
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)
    token = token_factory(
        liquidity_usd=20_000,
        boost_total_amount=500.0,
    )
    points, signals = score(token, settings)
    # Condition is `>= MIN_BOOST_TOTAL_AMOUNT` per spec §6.
    assert "velocity_boost" in signals


def test_velocity_boost_contributes_exactly_20_raw_points(
    token_factory, settings_factory
):
    """Diff-test: a token scored with vs without boost_total_amount differs
    by exactly +20 raw points (pre-normalization)."""
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)

    # Minimal token that triggers NO other signals — market_cap, age,
    # growth, buys, momentum all zero/None. Just above liquidity floor.
    base_kwargs = dict(
        contract_address="0xdiff",
        chain="ethereum",  # avoid solana_bonus
        token_name="X",
        ticker="X",
        market_cap_usd=0,
        liquidity_usd=20_000,
        volume_24h_usd=0,  # no vol_liq_ratio
        holder_count=0,
        holder_growth_1h=0,
        token_age_days=100,  # past peak
    )
    t_no_boost = token_factory(**base_kwargs, boost_total_amount=None)
    t_boost = token_factory(**base_kwargs, boost_total_amount=1500.0)

    pts_no, sig_no = score(t_no_boost, settings)
    pts_yes, sig_yes = score(t_boost, settings)

    assert "velocity_boost" not in sig_no
    assert "velocity_boost" in sig_yes
    # Raw-point recovery via SCORER_MAX_RAW * normalized / 100 is imprecise;
    # instead assert that the ONLY signal that fired in pts_yes is velocity_boost,
    # and pts_yes normalized == int(20 * 100 / 203) == 9.
    assert sig_yes == ["velocity_boost"]
    assert pts_yes == int(20 * 100 / 203)  # == 9
    assert pts_no == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scorer_velocity_boost.py -v`
Expected: FAIL — `SCORER_MAX_RAW == 203` fails (currently 183); `velocity_boost` not in any signals list.

- [ ] **Step 3: Update scorer docstring + constant**

In `scout/scorer.py`, replace the existing module docstring (lines 1-26) in full with:

```python
"""Quantitative scoring engine for candidate tokens.

Scoring weights (must always document rationale):
- vol_liq_ratio (>MIN_VOL_LIQ_RATIO): 30 points -- Primary pump precursor
- market_cap_range (tiered: 8/5/2 pts): Pre-discovery range
- holder_growth (>20 new/hour): 25 points -- Organic accumulation
- token_age (bell curve, peak 12-48h): 0-15 points -- Early stage
- social_mentions (>50 in 24h): 15 points -- CT discovery signal (optional)

DexScreener signals:
- buy_pressure (buy_ratio > BUY_PRESSURE_THRESHOLD): 15 points -- Organic buying vs wash trade
- velocity_boost (boost_total_amount >= MIN_BOOST_TOTAL_AMOUNT): 20 points -- Paid-promo momentum (BL-051)

CoinGecko signals:
- momentum_ratio (1h/24h > MOMENTUM_RATIO_THRESHOLD): 20 points -- Accelerating
- vol_acceleration (vol/7d_avg > MIN_VOL_ACCEL_RATIO): 25 points -- Volume spike
- cg_trending_rank (rank <= 10): 15 points -- Social discovery

Velocity signal:
- score_velocity (rising over 3 scans): 10 points -- Active accumulation

Chain bonus:
- solana_bonus (chain == solana): 5 points -- Meme premium

Max raw: 30+8+25+15+15+15+20+25+15+20+5+10 = 203 points
Normalized to 0-100 scale, then co-occurrence multiplier (1.15x if 3+ signals) applied.
"""
```

Then update the constant (was line 32):

```python
# Theoretical maximum raw score — update if signal weights change
SCORER_MAX_RAW = 203
```

- [ ] **Step 4: Insert Signal 10 (velocity_boost) and renumber 11/12**

In `scout/scorer.py`, immediately after the existing Signal 9 block (ends around line 145, `signals.append("cg_trending_rank")`) and BEFORE the existing `# Signal 10: Solana chain bonus` block, insert:

```python
    # Signal 10: Velocity boost (DexScreener top-boosts cumulative) -- 20 points (BL-051)
    if (
        token.boost_total_amount is not None
        and token.boost_total_amount >= settings.MIN_BOOST_TOTAL_AMOUNT
    ):
        points += 20
        signals.append("velocity_boost")
```

Then renumber the two subsequent comment headers:

- Change `# Signal 10: Solana chain bonus -- 5 points` to `# Signal 11: Solana chain bonus -- 5 points`.
- Change `# Signal 11: Score velocity bonus -- 10 points` to `# Signal 12: Score velocity bonus -- 10 points`.

- [ ] **Step 5: Run new tests to verify they pass**

Run: `uv run pytest tests/test_scorer_velocity_boost.py -v`
Expected: PASS (all 5 new tests green).

- [ ] **Step 6: Audit legacy `test_scorer.py` for normalization drift**

The `SCORER_MAX_RAW` constant changed from 183 → 203. Three assertions in `tests/test_scorer.py` bake in the old value.

Run: `uv run pytest tests/test_scorer.py -v`
Expected: FAIL on `test_max_raw_sums_to_correct_total` (line 1031 asserts 183) and `test_single_signal_normalization` (line 1046 asserts `int(30*100/183)`). Other tests that compare computed `points` against integer literals may also drift by 1–2 points.

- [ ] **Step 7: Fix `test_scorer.py` golden values**

In `tests/test_scorer.py`:

1. Line 1030 comment — replace `30+8+25+15+15+15+20+25+15+5+10 = 183` with `30+8+25+15+15+15+20+25+15+20+5+10 = 203`.
2. Line 1031 — replace `assert SCORER_MAX_RAW == 183` with `assert SCORER_MAX_RAW == 203`.
3. Line 1034 docstring — replace `int(30*100/183)=16` with `int(30*100/203)=14`.
4. Line 1046 assertion — replace `int(30 * 100 / 183)` with `int(30 * 100 / 203)`.

Do NOT update lines 49/143/172/188/204/367/439/455/542/927 — those are `# raw=..., normalized=...` *explanatory comments*, harmless as-is. If any hard-coded `assert points == N` values fail after step 6, compute the new expected with `int(raw_points * 100 / 203)` (multiplied by `1.15` and `int()`'d if the test involves the co-occurrence multiplier) and update inline.

- [ ] **Step 8: Re-run the full scorer test module**

Run: `uv run pytest tests/test_scorer.py tests/test_scorer_velocity_boost.py -v`
Expected: PASS — all scorer tests green.

- [ ] **Step 9: Commit**

```bash
git add scout/scorer.py tests/test_scorer.py tests/test_scorer_velocity_boost.py
git commit -m "feat(bl-051): add velocity_boost signal; bump SCORER_MAX_RAW to 203"
```

---

### Task 7: Wire `fetch_top_boosts` into `run_cycle`

**Files:**
- Modify: `scout/main.py:34` (import), lines 315-324 (gather), lines 326-340 (exception branches), line 455 (decorator call after `aggregate`).
- Test: `tests/test_main_pipeline_top_boosts.py` (new integration test)

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_main_pipeline_top_boosts.py`:

```python
"""BL-051 integration: a candidate that appears on /token-boosts/top gets
`velocity_boost` fired in its signals."""

from unittest.mock import AsyncMock, patch

import pytest

from scout.aggregator import apply_boost_decorations
from scout.ingestion.dexscreener import BoostInfo
from scout.scorer import score


@pytest.mark.asyncio
async def test_decorated_candidate_fires_velocity_boost(token_factory, settings_factory):
    """Minimal integration: decorate a candidate via apply_boost_decorations
    and confirm the scorer picks it up end-to-end."""
    settings = settings_factory(MIN_BOOST_TOTAL_AMOUNT=500.0)
    cand = token_factory(
        contract_address="0xfeed",
        chain="ethereum",
        liquidity_usd=20_000,
    )
    boosts = [BoostInfo(chain="ethereum", address="0xfeed", total_amount=1500.0)]

    decorated = apply_boost_decorations([cand], boosts)
    assert decorated[0].boost_total_amount == 1500.0

    _, signals = score(decorated[0], settings)
    assert "velocity_boost" in signals


@pytest.mark.asyncio
async def test_run_cycle_includes_top_boosts_fetch(settings_factory, tmp_path):
    """Verify run_cycle invokes fetch_top_boosts and wires its output
    through apply_boost_decorations."""
    from scout import main as main_module
    from scout.db import Database
    import aiohttp

    settings = settings_factory(DB_PATH=tmp_path / "test.db")
    db = Database(settings.DB_PATH)
    await db.initialize()

    with patch.object(main_module, "fetch_trending", new=AsyncMock(return_value=[])), \
         patch.object(main_module, "fetch_trending_pools", new=AsyncMock(return_value=[])), \
         patch.object(main_module, "cg_fetch_top_movers", new=AsyncMock(return_value=[])), \
         patch.object(main_module, "cg_fetch_trending", new=AsyncMock(return_value=[])), \
         patch.object(main_module, "cg_fetch_by_volume", new=AsyncMock(return_value=[])), \
         patch.object(
             main_module,
             "fetch_top_boosts",
             new=AsyncMock(return_value=[BoostInfo("ethereum", "0xfeed", 1500.0)]),
         ) as mock_top_boosts, \
         patch.object(
             main_module,
             "apply_boost_decorations",
             wraps=main_module.apply_boost_decorations,
         ) as mock_apply:
        async with aiohttp.ClientSession() as session:
            await main_module.run_cycle(settings, db, session, dry_run=True)

    await db.close()

    assert mock_top_boosts.await_count == 1
    assert mock_apply.call_count == 1
    # The wired-through boost list must reach the decorator unchanged.
    call_args = mock_apply.call_args
    passed_boosts = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["boosts"]
    assert len(passed_boosts) == 1
    assert passed_boosts[0].address == "0xfeed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_main_pipeline_top_boosts.py -v`
Expected: FAIL — `test_decorated_candidate_fires_velocity_boost` may pass already (previous tasks), BUT `test_run_cycle_includes_top_boosts_fetch` FAILS because `main.fetch_top_boosts` and `main.apply_boost_decorations` are not yet imported/wired.

- [ ] **Step 3: Add imports and wire gather**

In `scout/main.py`, line 34 (currently `from scout.ingestion.dexscreener import fetch_trending`), append a second import on a new line after it:

```python
from scout.ingestion.dexscreener import fetch_top_boosts
```

Locate the `from scout.aggregator import aggregate` line (near the other `scout.` imports at the top of `main.py`) and replace it with:

```python
from scout.aggregator import aggregate, apply_boost_decorations
```

(If the import is currently a separate single-name import, adjust accordingly — confirm line number by grepping for `from scout.aggregator`.)

- [ ] **Step 4: Extend the Stage-1 `asyncio.gather`**

In `scout/main.py`, replace the existing block at lines 315-324:

```python
    # Stage 1: Parallel ingestion
    dex_tokens, gecko_tokens, cg_movers, cg_trending, cg_by_volume = (
        await asyncio.gather(
            fetch_trending(session, settings),
            fetch_trending_pools(session, settings),
            cg_fetch_top_movers(session, settings),
            cg_fetch_trending(session, settings),
            cg_fetch_by_volume(session, settings),
            return_exceptions=True,
        )
    )
```

...with the 6-arg version:

```python
    # Stage 1: Parallel ingestion
    (
        dex_tokens,
        gecko_tokens,
        cg_movers,
        cg_trending,
        cg_by_volume,
        top_boosts,
    ) = await asyncio.gather(
        fetch_trending(session, settings),
        fetch_trending_pools(session, settings),
        cg_fetch_top_movers(session, settings),
        cg_fetch_trending(session, settings),
        cg_fetch_by_volume(session, settings),
        fetch_top_boosts(session, settings),
        return_exceptions=True,
    )
```

- [ ] **Step 5: Add exception-guard branch**

In `scout/main.py`, immediately after the existing `if isinstance(cg_by_volume, Exception):` branch (ending with `cg_by_volume = []` at line 340), insert:

```python
    if isinstance(top_boosts, Exception):
        logger.warning("DexScreener top-boosts ingestion failed", error=str(top_boosts))
        top_boosts = []
```

- [ ] **Step 6: Call decorator after `aggregate`**

In `scout/main.py`, replace the block at lines 455-461 (the `aggregate(...)` call):

```python
    # Stage 2: Aggregate
    all_candidates = aggregate(
        list(dex_tokens)
        + list(gecko_tokens)
        + list(cg_movers)
        + list(cg_trending)
        + list(cg_by_volume)
    )
    stats["tokens_scanned"] = len(all_candidates)
```

...with:

```python
    # Stage 2a: Aggregate (dedup by contract_address)
    all_candidates = aggregate(
        list(dex_tokens)
        + list(gecko_tokens)
        + list(cg_movers)
        + list(cg_trending)
        + list(cg_by_volume)
    )
    # Stage 2b: Decorate with DexScreener top-boosts data (BL-051).
    # top_boosts is a list[BoostInfo] (possibly empty); harmless no-op when empty.
    all_candidates = apply_boost_decorations(all_candidates, list(top_boosts))
    stats["tokens_scanned"] = len(all_candidates)
```

- [ ] **Step 7: Run integration tests**

Run: `uv run pytest tests/test_main_pipeline_top_boosts.py -v`
Expected: PASS (both tests green).

- [ ] **Step 8: Run full test suite for regression check**

Run: `uv run pytest --tb=short -q`
Expected: PASS — entire suite green; no regressions. If any `test_main*.py` test asserts gather-tuple structure or ingestion-source counts, update the expectation to include the new 6th source.

- [ ] **Step 9: Commit**

```bash
git add scout/main.py tests/test_main_pipeline_top_boosts.py
git commit -m "feat(bl-051): wire fetch_top_boosts into run_cycle pipeline"
```

---

### Task 8: Smoke + format + final verification

**Files:**
- None — verification only.

- [ ] **Step 1: Run black formatter**

Run: `uv run black scout/ tests/`
Expected: `All done!` — no changes OR a small formatting delta on the new code. If files are reformatted, commit them:

```bash
git add scout/ tests/
git commit -m "style(bl-051): black formatting"
```

- [ ] **Step 2: Dry-run the pipeline once**

Run: `uv run python -m scout.main --dry-run --cycles 1`
Expected: Process exits 0 within ~90s. `dex_top_boosts_fetched` log line appears exactly once. No new `warning`/`error` log-level events that did not already exist on master.

If `dex_top_boosts_fetched` does NOT appear, the wiring is broken — re-check Task 7 Step 4/5.

- [ ] **Step 3: Full test suite**

Run: `uv run pytest --tb=short -q`
Expected: PASS — zero failures, zero errors.

- [ ] **Step 4: Final commit (if any formatting changes were staged but uncommitted)**

If `git status` shows staged changes, commit them:

```bash
git add -A
git commit -m "style(bl-051): final formatting pass"
```

Otherwise, skip this step.

---

## Spec Traceability

| Spec Section | Task(s) |
|---|---|
| §4 Architecture / pipeline diagram | Task 7 |
| §5 Data Model Changes (boost_total_amount, boost_rank) | Task 1 |
| §6 Scorer Change (velocity_boost, SCORER_MAX_RAW=203) | Task 6 |
| §7 Config Settings (MIN_BOOST_TOTAL_AMOUNT, POLL_EVERY_CYCLES) | Task 2 |
| §8 API Integration Details (endpoint, _get_json reuse, schema drift) | Task 4 |
| §9 Aggregator Semantics (apply_boost_decorations, normalization) | Task 3, Task 5 |
| §9 main.py wiring (gather + exception branch + decorator call) | Task 7 |
| §10 Observability (dex_top_boosts_fetched log) | Task 4 |
| §11 Error Handling (empty-list on upstream failure; skip schema drift) | Task 4 |
| §12 Testing (7 unit tests, 8 aggregator, 5 scorer, 2 integration) | Tasks 3-7 |
| §13 Acceptance Criteria (AC1-AC9) | All tasks collectively; Task 8 smoke |
| §16 Implementation Checklist | All tasks |
