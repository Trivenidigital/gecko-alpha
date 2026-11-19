# gecko Plan 2 — GoPlus enrichment + Telegram callers Implementation Plan

**New primitives introduced:** `gecko/collectors/goplus.py` (`enrich` job, GoPlus keyless security + holders); `gecko/calls.py` (call parser, `insert_call`, `resolve_calls`, `call_seeds`); `gecko/telegram_reader.py` (`telegram-reader` long-running poll loop, `tg-login`, `migrate-calls` jobs); `gecko/prune.py` (`prune` job); `Store.set_deployer`, `Store.close_unmeasured`; DexScreener `telegram` discovery seed feed; freshness keys `observations:enrich` and run-anchored `source_calls`; settings `gp_*`, `tg_*`, `obs_retention_days`, `runs_retention_days`, `ledger_max_overdue_days`; backtest `--from/--to`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Plan 1 watchlist its two missing input lanes — per-token security/holder enrichment and Telegram caller calls — plus the track-record and disk tickets from the Plan 1 reviews, without deploying anything.

**Architecture:** One deterministic `enrich` script job calls the keyless GoPlus API once per token (one address per call) and writes `source="enrich"` observations, which `gecko/features.py` already reads (`security_ok`, `holders`, `top10_pct`). A long-running `telegram-reader` keeps its own Telethon user session open and polls each configured channel every 2 minutes; each poll inserts parsed calls into `source_calls`, resolves them to known tokens, and writes one `runs` row so a dead reader shows in freshness. Unresolved called addresses become a third DexScreener discovery feed, so Telegram is a real discovery source (spec S2).

**Tech Stack:** Python 3.12, aiohttp, aiosqlite, pydantic-settings, structlog, Telethon (`>=1.36,<1.40`, already pinned), pytest + pytest-asyncio (auto mode) + aioresponses.

**Spec:** `docs/superpowers/specs/2026-10-09-gecko-lean-hermes-agent-design.md` (amended by Task 0 of this plan). Plan 1: `docs/superpowers/plans/2026-10-09-gecko-core-package.md`; rulings `…-rulings.md`; tickets `…-followups.md`.

## Hermes-first analysis

Inherits the spec's table (§ Hermes-first). Re-checked 2026-10-14 for the two domains this plan builds:

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Token security / holders (deterministic write path) | GoPlus official MCP exists, but MCP tools are agent-side; spec §2 forbids an LLM in any data write path | build a thin REST collector (`gecko/collectors/goplus.py`); the MCP stays available to the research agent (Plan 3) |
| Solana holders (Helius) / EVM holders (Blockscout) | Helius MCP, Blockscout MCP | not used by the write path: probed 2026-10-14, GoPlus keyless already returns holder count + top-10 holders on all four chains; Blockscout `base.blockscout.com` returned a Cloudflare challenge (403) and no `bsc.blockscout.com` exists (404). Both stay agent-side MCP tools in Plan 3 |
| Telegram channel reading (user account) | none — gateway is Bot API only | build thin poll loop (`gecko/telegram_reader.py`), ~150 lines vs the legacy 1,801-line listener |

awesome-hermes-agent: no Telethon/user-session reader or deterministic GoPlus writer found (same lists as the spec). Verdict: extends-Hermes; custom code limited to the two collectors above.

## Operator rulings this plan rests on (2026-10-14, AskUserQuestion)

1. Spec S1 cap raised to **gecko/ ≤ 3,600 lines, tests/gecko ≤ 7,500** (was 3,000 / 6,000). Reason: enrichment + Telegram were always in scope but absent from the original estimate.
2. Enrichment = **GoPlus only** (keyless). Helius/Blockscout become agent-side MCP only.
3. Telegram reader = **long-running poll loop** (one systemd unit in Plan 3), not Hermes cron, not an event handler.
4. Tickets in scope = **track-record + disk only**: prune job, ledger auto-finalizer, CoinGecko-fallback-after-deadline, backtest `--from/--to` + exit 1 on FAIL. Every other Plan-2-tagged ticket stays in `…-followups.md`.

## Empirical facts this plan depends on (probed 2026-10-14 from the dev box)

- `GET https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses=<addr>` and `GET https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses=<mint>` both return HTTP 200 **without a key**. Chain ids: ethereum `1`, bsc `56`, base `8453`.
- Body shape: `{"code": 1, "message": "OK", "result": {"<address>": {...}}}`. EVM result keys are lowercase.
- **Batching is silently truncated on the free tier**: two comma-separated addresses returned ONE result, still `code: 1`. One address per call.
- **An unknown token returns `code: 1` with `"result": {}`** — it must read as "unknown", never "safe".
- Free tier (GoPlus pricing page): 150 CU/min, 30K CU/day, 150K CU/month. The CU cost of one `token_security` call is not published; Plan 3 soak calibrates `gp_daily_cap` from the GoPlus dashboard.
- EVM fields used: `is_honeypot`, `cannot_sell_all`, `is_blacklisted`, `sell_tax`, `buy_tax` (fractions as strings, `""` when unknown), `is_mintable`, `is_proxy`, `owner_address`, `holder_count`, `holders[]` (`address`, `percent` as a FRACTION string e.g. `"0.2261"`, `is_contract` 0/1, `is_locked` 0/1), `lp_holders[]`, `dex[].pair`, `creator_address`.
- Solana fields used: `mintable.status`, `freezable.status`, `balance_mutable_authority.status`, `non_transferable`, `transfer_hook` (list), `trusted_token`, `holder_count`, `holders[]` (`account`, `percent` fraction, `is_locked`), `dex[].id`.
- **Concentration trap**: BRETT (base, a large legitimate token) has raw top-10 share ≈ 62% because its #1 holder is a contract (22.6%); WIF's #10 holder is the Raydium AMM authority `5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1`. Counting every top-10 holder would disqualify real tokens at the 60% cap. Excluding EVM contracts, locked balances, burn addresses and pool addresses gives BRETT ≈ 22%.
- Legacy Telegram history lives in `tg_social_messages(channel_handle, msg_id, posted_at, text, …, UNIQUE(channel_handle, msg_id))`; active channels in `tg_social_channels(channel_handle, removed_at)`.

## Global Constraints

- Code style: black, `target-version = ["py312"]`; every `gecko/**/*.py` must parse with `ast.parse(feature_version=(3, 12))` (guarded by `tests/gecko/test_gecko_py312_syntax.py`).
- Size: `gecko/` ≤ 3,600 non-blank non-comment lines; `tests/gecko` ≤ 7,500 (`scripts/gecko_loc_guard.py`, after Task 0).
- Test files: `tests/gecko/test_gecko_<name>.py` (no `__init__.py` in `tests/gecko`).
- Settings: `GECKO_*` env vars / `.env.gecko` only — never `.env`.
- Async everywhere (aiohttp, aiosqlite); no `requests`; no `print()` except CLI usage/refusal messages to stderr (existing `__main__` pattern) and the backtest/migrate report lines.
- Every stored timestamp goes through `canon_ts()`; every new job is registered via `gecko.jobs.register` and imported in `gecko/__main__.py::_load_jobs`.
- Transport/exception logs record the exception TYPE (and status) only — never `str(exc)` of an aiohttp error, never `UpstreamError.body`.
- LLM never in a write path; nothing in this plan calls a model.
- Telethon `*.session` files: absolute path, outside the repo, mode 0600, never committed.
- Nothing is deployed by this plan. Deploy is Plan 3 with its own approval.

## Review Focus

1. **GoPlus "unknown" read as "safe"** — an empty `result`, a `code` other than 1/2, or a missing core flag must yield `security_ok = None` (never `True`). Pinned in Task 1 (`test_unknown_and_partial_results_are_none`) and Task 2 (`test_empty_result_writes_unknown_marker`).
2. **Concentration over-fires on legitimate tokens** — contracts/LP/locked/burn/AMM-authority holders inside the top-10 must be excluded before the 40%/60% thresholds. Pinned in Task 1 with the probed BRETT and WIF shapes.
3. **Prune deletes what the ledger still needs** — the 180-day horizon looks up BTC at `entered_at`, 180+ days in the past. `benchmark:btc` rows are never pruned; retention below 30 days is refused. Pinned in Task 7 (`test_benchmark_rows_survive_prune`, `test_retention_floor_validated`).
4. **Telegram reader dies silently or resolves usernames every poll** — a failing channel must make the poll's `runs` row `ok=0` (freshness pages), and usernames are resolved once at start (Telegram rate-limits `ResolveUsername` hard). Pinned in Task 5 (`test_failed_channel_marks_run_not_ok`, `test_entities_resolved_once`).
5. **Session collision with the legacy listener** — this plan uses a SEPARATE session file created by `tg-login` (Telegram treats it as another device), so the legacy `tg_social.session` is never opened by gecko. The reader refuses a session path inside the repo, a relative path, or a group/world-readable file. Pinned in Task 5 (`test_session_problems`).

---

## File map

| File | Status | Responsibility |
|---|---|---|
| `scripts/gecko_loc_guard.py` | modify | limits 3,600 / 7,500 |
| `tests/gecko/test_gecko_loc_guard.py` | modify | new limits |
| `docs/superpowers/specs/2026-10-09-gecko-lean-hermes-agent-design.md` | modify | amendments (S1, §5, §6 top10, §11, §12, §14.4) |
| `gecko/settings.py` | modify | `gp_*`, `tg_*`, retention, ledger expiry settings |
| `gecko/collectors/goplus.py` | create | GoPlus parsing (pure) + `enrich` job |
| `gecko/store.py` | modify | `set_deployer`, `close_unmeasured` |
| `gecko/freshness.py` | modify | `observations:enrich`; `source_calls` run-anchored |
| `gecko/calls.py` | create | call parsing, insert, resolution, discovery seeds |
| `gecko/collectors/dexscreener.py` | modify | merge Telegram seeds into discovery |
| `gecko/telegram_reader.py` | create | `telegram-reader`, `tg-login`, `migrate-calls` jobs |
| `gecko/prune.py` | create | `prune` job |
| `gecko/ledger.py` | modify | CG fallback only after deadline; overdue expiry |
| `gecko/backtest/runner.py`, `gecko/backtest/study.py` | modify | `--from/--to`; exit 1 on FAIL |
| `gecko/__main__.py` | modify | import new job modules; `--from/--to` args |
| `tests/gecko/test_gecko_goplus.py`, `test_gecko_calls.py`, `test_gecko_telegram.py`, `test_gecko_prune.py`, `test_gecko_seam.py` | create | tests |

---

### Task 0: Size cap + spec amendments

**Files:**
- Modify: `scripts/gecko_loc_guard.py` (docstring line 2, `LIMITS`)
- Modify: `tests/gecko/test_gecko_loc_guard.py`
- Modify: `docs/superpowers/specs/2026-10-09-gecko-lean-hermes-agent-design.md`

**Interfaces:** Produces the 3,600 / 7,500 limits every later task is measured against.

- [ ] **Step 1: Update the guard test first (fails against the old limits)**

In `tests/gecko/test_gecko_loc_guard.py` replace the limit literals:

```python
# test_guard_passes_on_current_tree
    m = re.search(r"gecko=(\d+)/3600 tests=(\d+)/7500", p.stdout)

# test_guard_fails_when_limit_exceeded
    (big / "x.py").write_text("\n".join(f"a{i} = {i}" for i in range(3700)))
    ...
    assert "gecko=3700/3600" in p.stdout

# test_guard_skips_pycache_and_non_py  (3100-line decoys stay; only the expected line changes)
    assert "gecko=1/3600 tests=0/7500" in p.stdout

# test_guard_passes_at_exact_gecko_limit
    (tmp_path / "gecko" / "x.py").write_text(_lines(3600))
    ...
    assert "gecko=3600/3600" in p.stdout

# test_guard_fails_when_tests_limit_exceeded
    (t / "test_x.py").write_text(_lines(7501))
    ...
    assert "gecko=10/3600 tests=7501/7500" in p.stdout
```

Add a boundary test for the tests limit (ticketed in Plan 1 follow-ups, "no boundary test at exactly 5,000 test lines"):

```python
def test_guard_passes_at_exact_tests_limit(tmp_path):
    (tmp_path / "gecko").mkdir()
    (tmp_path / "gecko" / "x.py").write_text(_lines(10))
    t = tmp_path / "tests" / "gecko"
    t.mkdir(parents=True)
    (t / "test_x.py").write_text(_lines(7500))
    p = _run("--root", str(tmp_path))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "gecko=10/3600 tests=7500/7500" in p.stdout
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_loc_guard.py -q`
Expected: FAIL (`gecko=…/3000` does not match `/3600`).

- [ ] **Step 3: Raise the limits**

`scripts/gecko_loc_guard.py`:

```python
"""Spec S1 guard: gecko/ <= 3600 code lines, tests/gecko <= 7500. Exit 1 on breach."""
...
LIMITS = {"gecko": 3600, "tests/gecko": 7500}
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko/test_gecko_loc_guard.py -q && python scripts/gecko_loc_guard.py`
Expected: all pass; guard prints `gecko=2891/3600 tests=50xx/7500`.

- [ ] **Step 5: Amend the spec**

Edit the spec in place (each amendment dated 2026-10-14, same style as the 10-09 amendments):

- §1 S1 row: append "raised 3,000 → 3,600 and tests 6,000 → 7,500 on 2026-10-14 (operator ruling: GoPlus enrichment, Telegram reader, prune and ledger tickets were in scope but not in the estimate)".
- Hermes-first table: Solana row and EVM row decisions become "agent-side MCP only (Plan 3); the deterministic write path uses GoPlus, which returns holders + top-10 on all four chains keyless (probed 2026-10-14; Blockscout base is Cloudflare-blocked and has no BSC instance)".
- §5 collectors table: replace the Helius and Blockscout rows with one row — `GoPlus (REST, keyless) | token_security/{1,56,8453}, solana/token_security, ONE address per call (free tier silently truncates batches) | hourly job; first sight for candidates, daily for active/watchlist, daily retry while GoPlus does not know the token | free 150 CU/min, 30K CU/day; gp_daily_cap enforced from runs | security_ok, holders, top10_pct, deployer | all`. Keep the GoPlus MCP for the agent in §11.
- §6 `top10_pct` row: "sum of the `percent` of GoPlus's top-10 holders, ×100, EXCLUDING locked balances, burn addresses, pool addresses (`dex[].pair`/`dex[].id`, `lp_holders`), EVM contracts (`is_contract=1`) and known Solana AMM authorities. Holders beyond the reported 10 are not backfilled, so this is a lower bound."
- §6 `security_ok` row: "False if any: EVM honeypot / cannot_sell_all / blacklist / buy or sell tax > 10% / mintable or proxy with a non-renounced owner; Solana mintable / freezable / balance-mutable authority / non-transferable / transfer hook (a `trusted_token` overrides to True). True only when the core flags are present and clean; None otherwise (unknown is never safe)."
- §8/§9: no change.
- §12 freshness: `source_calls` becomes run-anchored on `telegram-reader` ok runs with a 30-minute SLO (channels may be legitimately silent for days; a reader that cannot poll writes `ok=0` runs and pages). Add `observations:enrich` 26 h.
- §14.4: replace with "gecko uses its OWN Telethon session created by `python -m gecko tg-login` (a second device on the same account), so there is no session handover; the legacy listener is stopped at retirement (step 7) like every other unit."

- [ ] **Step 6: Commit**

```bash
git add scripts/gecko_loc_guard.py tests/gecko/test_gecko_loc_guard.py docs/superpowers/specs/2026-10-09-gecko-lean-hermes-agent-design.md
git commit -m "docs(gecko): Plan 2 rulings — S1 to 3600/7500, GoPlus-only enrichment, own TG session"
```

---

### Task 1: GoPlus response parsing (pure)

**Files:**
- Create: `gecko/collectors/goplus.py` (parsing half)
- Test: `tests/gecko/test_gecko_goplus.py`

**Interfaces:**
- Consumes: `gecko.numeric.num`, `as_int`; `gecko.identity.EVM_CHAINS`, `is_valid_address`, `norm_address`.
- Produces:
  - `GOPLUS_EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"`, `GOPLUS_SOL_URL = "https://api.gopluslabs.io/api/v1/solana/token_security"`, `CHAIN_IDS = {"ethereum": "1", "bsc": "56", "base": "8453"}`
  - `pick_result(chain: str, address: str, body) -> dict | None` — the token's entry from a GoPlus body; `None` for an unknown token (empty result) — raises `GoPlusError(code)` when `body["code"]` is not 1 or 2.
  - `class GoPlusError(Exception)` with attribute `code`.
  - `goplus_features(chain: str, r: dict) -> dict` returning exactly the keys `goplus_status` (`"ok"`), `security_ok` (`bool | None`), `security_flags` (`list[str]`, sorted), `holders` (`int | None`), `top10_pct` (`float | None`, percent 0–100), `deployer` (`str | None`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/gecko/test_gecko_goplus.py
import pytest

from gecko.collectors.goplus import GoPlusError, goplus_features, pick_result

BRETT = "0x532f27101965dd16442e59d40670faf5ebb142e4"
POOL = "0x0ca6485b7e9cf814a3fd09d81672b07323535b64"
RAYDIUM_AUTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


def evm(**over):
    r = {
        "is_honeypot": "0",
        "cannot_sell_all": "0",
        "is_blacklisted": "0",
        "buy_tax": "0",
        "sell_tax": "0",
        "is_mintable": "0",
        "is_proxy": "0",
        "owner_address": "",
        "holder_count": "1182384",
        "creator_address": "0x3C12B77aE8B7DD1FEB63D1D6a2A819AcdA0a41d2",
        "dex": [{"pair": POOL}],
        "lp_holders": [],
        # probed BRETT shape: #1 is a contract with 22.6%
        "holders": [
            {"address": "0x06a19654e0872ba71c2261ea691ecf8a0c677156", "percent": "0.2262", "is_contract": 1, "is_locked": 0},
            {"address": "0x82d4cd9bf16017fd518511d8bbe6d0493af31a10", "percent": "0.0562", "is_contract": 0, "is_locked": 0},
            {"address": "0xefef4558802bf373ce3307189c79a9cab0a4cb9c", "percent": "0.0544", "is_contract": 1, "is_locked": 0},
            {"address": "0x9773c9e7d048ae91a4fd9a48cd0b984e31fa418c", "percent": "0.0530", "is_contract": 0, "is_locked": 0},
            {"address": "0x000000000000000000000000000000000000dead", "percent": "0.1000", "is_contract": 0, "is_locked": 0},
            {"address": POOL, "percent": "0.0900", "is_contract": 0, "is_locked": 0},
            {"address": "0x4cc69ff32638a7e2b5e2f0c4e6de6b0ab7f9b6c8", "percent": "0.0479", "is_contract": 0, "is_locked": 1},
        ],
    }
    r.update(over)
    return r


def sol(**over):
    r = {
        "mintable": {"status": "0"},
        "freezable": {"status": "0"},
        "balance_mutable_authority": {"status": "0"},
        "non_transferable": "0",
        "transfer_hook": [],
        "trusted_token": 0,
        "holder_count": "262738",
        "dex": [{"id": "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx"}],
        "holders": [
            {"account": "51yZyDSnec4xnUv7XLRVYcDyV4x3wUtzrDcRaYbmQU5j", "percent": "0.1372", "is_locked": 0},
            {"account": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "percent": "0.0967", "is_locked": 0},
            {"account": RAYDIUM_AUTH, "percent": "0.0143", "is_locked": 0},
            {"account": "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx", "percent": "0.0500", "is_locked": 0},
        ],
    }
    r.update(over)
    return r


def test_evm_clean_token():
    f = goplus_features("base", evm())
    assert f["goplus_status"] == "ok"
    assert f["security_ok"] is True and f["security_flags"] == []
    assert f["holders"] == 1182384
    assert f["deployer"] == "0x3c12b77ae8b7dd1feb63d1d6a2a819acda0a41d2"


def test_evm_top10_excludes_contracts_locked_burn_and_pools():
    # only the two EOAs count: 5.62% + 5.30%
    assert goplus_features("base", evm())["top10_pct"] == pytest.approx(10.92)


def test_evm_top10_also_excludes_lp_holders():
    r = evm(lp_holders=[{"address": "0x82d4cd9bf16017fd518511d8bbe6d0493af31a10"}])
    assert goplus_features("base", r)["top10_pct"] == pytest.approx(5.30)


@pytest.mark.parametrize(
    "over,flag",
    [
        ({"is_honeypot": "1"}, "honeypot"),
        ({"cannot_sell_all": "1"}, "cannot_sell_all"),
        ({"is_blacklisted": "1"}, "blacklist"),
        ({"sell_tax": "0.11"}, "sell_tax"),
        ({"buy_tax": "0.5"}, "buy_tax"),
        ({"is_mintable": "1", "owner_address": "0x1111111111111111111111111111111111111111"}, "mintable_owned"),
        ({"is_proxy": "1", "owner_address": "0x1111111111111111111111111111111111111111"}, "proxy_owned"),
    ],
)
def test_evm_each_disqualifier_names_itself(over, flag):
    f = goplus_features("base", evm(**over))
    assert f["security_ok"] is False and f["security_flags"] == [flag]


@pytest.mark.parametrize(
    "owner", ["", "0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"]
)
def test_evm_mintable_with_renounced_owner_is_ok(owner):
    f = goplus_features("base", evm(is_mintable="1", is_proxy="1", owner_address=owner))
    assert f["security_ok"] is True


def test_evm_tax_exactly_ten_percent_is_ok():
    assert goplus_features("base", evm(sell_tax="0.10"))["security_ok"] is True


def test_sol_clean_token_and_top10_excludes_amm_and_pool():
    f = goplus_features("solana", sol())
    assert f["security_ok"] is True and f["holders"] == 262738
    assert f["top10_pct"] == pytest.approx(23.39)  # 13.72 + 9.67
    assert f["deployer"] is None


@pytest.mark.parametrize(
    "over,flag",
    [
        ({"mintable": {"status": "1"}}, "mintable"),
        ({"freezable": {"status": "1"}}, "freezable"),
        ({"balance_mutable_authority": {"status": "1"}}, "balance_mutable"),
        ({"non_transferable": "1"}, "non_transferable"),
        ({"transfer_hook": [{"address": "x"}]}, "transfer_hook"),
    ],
)
def test_sol_each_disqualifier_names_itself(over, flag):
    f = goplus_features("solana", sol(**over))
    assert f["security_ok"] is False and f["security_flags"] == [flag]


def test_sol_trusted_token_overrides():
    f = goplus_features("solana", sol(mintable={"status": "1"}, trusted_token=1))
    assert f["security_ok"] is True


def test_unknown_and_partial_results_are_none():
    # core flag missing -> unknown, never safe
    assert goplus_features("base", evm(is_honeypot=None))["security_ok"] is None
    assert goplus_features("base", evm(is_honeypot=""))["security_ok"] is None
    assert goplus_features("solana", sol(freezable={}))["security_ok"] is None
    # a known failure still wins over a missing flag
    assert goplus_features("base", evm(is_honeypot=None, is_blacklisted="1"))["security_ok"] is False
    # malformed holders -> None, not a crash
    f = goplus_features("base", evm(holders="x", holder_count="n/a"))
    assert f["top10_pct"] is None and f["holders"] is None


def test_top10_none_when_no_holders_listed():
    assert goplus_features("base", evm(holders=[]))["top10_pct"] is None


def test_pick_result_matches_address_and_reads_unknown_as_none():
    body = {"code": 1, "result": {BRETT: {"is_honeypot": "0"}}}
    assert pick_result("base", BRETT.upper().replace("0X", "0x"), body) == {"is_honeypot": "0"}
    assert pick_result("base", BRETT, {"code": 1, "result": {}}) is None
    assert pick_result("solana", WIF, {"code": 2, "result": {WIF: {"a": 1}}}) == {"a": 1}
    # a different token's entry is never taken
    assert pick_result("base", BRETT, {"code": 1, "result": {POOL: {}}}) is None


def test_pick_result_raises_on_error_code():
    with pytest.raises(GoPlusError) as ei:
        pick_result("base", BRETT, {"code": 4029, "message": "too many requests"})
    assert ei.value.code == 4029
    with pytest.raises(GoPlusError):
        pick_result("base", BRETT, "not a dict")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_goplus.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'gecko.collectors.goplus'`.

- [ ] **Step 3: Implement the parser**

```python
# gecko/collectors/goplus.py
"""GoPlus token security + holders (keyless REST), one address per call.

The free tier silently drops every address after the first in a batch, and
answers an unknown token with ``code: 1`` and an empty result; both are
handled here so "unknown" can never read as "safe".
"""

from __future__ import annotations

from gecko.identity import EVM_CHAINS, is_valid_address, norm_address
from gecko.numeric import as_int, num

GOPLUS_EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
GOPLUS_SOL_URL = "https://api.gopluslabs.io/api/v1/solana/token_security"
CHAIN_IDS = {"ethereum": "1", "bsc": "56", "base": "8453"}
OK_CODES = (1, 2)  # 2 = partial data; still parsed, missing flags read as unknown
MAX_TAX = 0.10
RENOUNCED = {
    "",
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}
BURN = RENOUNCED | {"1nc1nerator11111111111111111111111111111111"}
# Solana AMM authorities that hold pool vaults (Raydium AMM v4, Raydium CPMM).
SOL_AMM_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",
}


class GoPlusError(Exception):
    def __init__(self, code):
        super().__init__(f"goplus code {code}")
        self.code = code


def pick_result(chain: str, address: str, body) -> dict | None:
    """This token's entry, or None when GoPlus does not know the token."""
    if not isinstance(body, dict) or body.get("code") not in OK_CODES:
        raise GoPlusError(body.get("code") if isinstance(body, dict) else None)
    want = norm_address(chain, address)
    for k, v in (body.get("result") or {}).items():
        if norm_address(chain, str(k)) == want and isinstance(v, dict):
            return v
    return None


def _flag(v) -> bool | None:
    """GoPlus "0"/"1" (str or int) -> bool; anything else -> None."""
    s = str(v) if v is not None else ""
    return {"0": False, "1": True}.get(s)


def _status(v) -> bool | None:
    return _flag(v.get("status")) if isinstance(v, dict) else None


def _evm_security(r: dict) -> tuple[list[str], bool]:
    owned = str(r.get("owner_address") or "").lower() not in RENOUNCED
    checks = {
        "honeypot": _flag(r.get("is_honeypot")),
        "cannot_sell_all": _flag(r.get("cannot_sell_all")),
        "blacklist": _flag(r.get("is_blacklisted")),
    }
    flags = [k for k, v in checks.items() if v]
    for key in ("buy_tax", "sell_tax"):
        tax = num(r.get(key))
        if tax is not None and tax > MAX_TAX:
            flags.append(key)
    if owned and _flag(r.get("is_mintable")):
        flags.append("mintable_owned")
    if owned and _flag(r.get("is_proxy")):
        flags.append("proxy_owned")
    return flags, checks["honeypot"] is not None


def _sol_security(r: dict) -> tuple[list[str], bool]:
    checks = {
        "mintable": _status(r.get("mintable")),
        "freezable": _status(r.get("freezable")),
        "balance_mutable": _status(r.get("balance_mutable_authority")),
        "non_transferable": _flag(r.get("non_transferable")),
    }
    flags = [k for k, v in checks.items() if v]
    hook = r.get("transfer_hook")
    if isinstance(hook, list) and hook:
        flags.append("transfer_hook")
    known = checks["mintable"] is not None and checks["freezable"] is not None
    return flags, known


def _top10(chain: str, r: dict) -> float | None:
    holders = r.get("holders")
    if not isinstance(holders, list) or not holders:
        return None
    skip = {norm_address(chain, str(d.get("pair") or d.get("id") or "")) for d in r.get("dex") or [] if isinstance(d, dict)}
    skip |= {norm_address(chain, str(h.get("address") or "")) for h in r.get("lp_holders") or [] if isinstance(h, dict)}
    skip |= BURN | SOL_AMM_AUTHORITIES
    total = 0.0
    for h in holders[:10]:
        if not isinstance(h, dict):
            continue
        addr = norm_address(chain, str(h.get("address") or h.get("account") or ""))
        if addr in skip or _flag(h.get("is_locked")):
            continue
        if chain in EVM_CHAINS and _flag(h.get("is_contract")):
            continue
        total += num(h.get("percent")) or 0.0
    return round(total * 100, 4)


def goplus_features(chain: str, r: dict) -> dict:
    if chain in EVM_CHAINS:
        flags, known = _evm_security(r)
        creator = str(r.get("creator_address") or "")
        deployer = norm_address(chain, creator) if is_valid_address(chain, creator) else None
    else:
        flags, known = _sol_security(r)
        deployer = None
        if _flag(r.get("trusted_token")):
            flags, known = [], True
    security_ok = False if flags else (True if known else None)
    return {
        "goplus_status": "ok",
        "security_ok": security_ok,
        "security_flags": sorted(flags),
        "holders": as_int(r.get("holder_count")),
        "top10_pct": _top10(chain, r),
        "deployer": deployer,
    }
```

Note for the implementer: black will reflow the long set-comprehensions in `_top10`; that is expected.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko/test_gecko_goplus.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gecko/collectors/goplus.py tests/gecko/test_gecko_goplus.py
git commit -m "feat(gecko): GoPlus security/holder parsing with unknown-is-not-safe and concentration exclusions"
```

---

### Task 2: `enrich` job

**Files:**
- Modify: `gecko/collectors/goplus.py` (job half)
- Modify: `gecko/settings.py`, `gecko/store.py`, `gecko/freshness.py`, `gecko/__main__.py`
- Test: `tests/gecko/test_gecko_goplus.py`, `tests/gecko/test_gecko_freshness.py`

**Interfaces:**
- Consumes: Task 1 (`pick_result`, `goplus_features`, `GoPlusError`, URLs, `CHAIN_IDS`); `gecko.http.get_json`, `Pacer`, `UpstreamError`; `gecko.runs.run_job`, `JobResult`, `PartialFailure`; `Store.add_observation`, `Store.set_token_status`.
- Produces:
  - settings `gp_min_interval_sec: float = 2.0`, `gp_max_per_run: int = 40`, `gp_daily_cap: int = 1000`, `gp_refresh_hours: int = 20`
  - `Store.set_deployer(token_id: str, deployer: str) -> None` (first value wins)
  - `async enrich_queue(store, settings, now) -> list[tuple[str, str, str]]` — `(token_id, chain, address)` in priority order
  - `async calls_today(store, now) -> int`
  - `async enrich(store, settings, session, now, *, sleep=asyncio.sleep, budget_now: str | None = None) -> JobResult`
  - job `enrich`
  - observation `source="enrich"`, features = `goplus_features(...)` minus `deployer`, or the marker `{"goplus_status": "unknown"}`
  - freshness key `observations:enrich` (SLO 1560 min, writer `enrich`)

Queue rule (one SQL + Python sort): a token in status `candidate` or `active` on a configured chain (never `benchmark`) is due when it has no enrich row, OR its last enrich row is older than `gp_refresh_hours` AND (it is `active`, or it has never had a `goplus_status = "ok"` row). Order: open-watchlist first, then `active`, then candidates; within each, never-enriched first, then oldest last row. A token whose result has `security_ok is False` is set `disqualified` / `security_fail` immediately (candidates never reach admission; actives leave at the next rank).

- [ ] **Step 1: Write the failing tests**

Append to `tests/gecko/test_gecko_goplus.py`:

```python
import aiohttp
from aioresponses import aioresponses

from gecko.collectors.goplus import calls_today, enrich, enrich_queue

NOW = "2026-10-14T12:00:00Z"
SOL_URL = "https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses="
BASE_URL = "https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses="
A = "0x" + "a" * 40
B = "0x" + "b" * 40


async def tok(store, chain, addr, status="active", first="2026-10-10T00:00:00Z"):
    tid = f"{chain}:{addr}"
    await store.upsert_token(tid, chain, addr, "T", "T", first, "dexscreener")
    await store.set_token_status(tid, status)
    return tid


async def enrich_rows(store, tid):
    return await store.observations(tid, as_of=NOW, source="enrich")


async def test_enrich_writes_features_and_deployer(store, settings):
    tid = await tok(store, "base", A)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: evm()}})
            r = await enrich(store, settings, s, NOW, sleep=_nosleep)
    assert r.rows_written == 1 and r.credits_used == 1
    (o,) = await enrich_rows(store, tid)
    assert o.features["security_ok"] is True and "deployer" not in o.features
    row = await (await store.conn.execute("SELECT deployer FROM tokens WHERE token_id=?", (tid,))).fetchone()
    assert row[0] == "0x3c12b77ae8b7dd1feb63d1d6a2a819acda0a41d2"


async def test_empty_result_writes_unknown_marker(store, settings):
    tid = await tok(store, "base", A)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {}})
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    (o,) = await enrich_rows(store, tid)
    assert o.features == {"goplus_status": "unknown"}


async def test_failed_security_disqualifies_candidate(store, settings):
    tid = await tok(store, "base", A, status="candidate")
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: evm(is_honeypot="1")}})
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    row = await (await store.conn.execute("SELECT status, disqualify_reason FROM tokens WHERE token_id=?", (tid,))).fetchone()
    assert row == ("disqualified", "security_fail")


async def test_queue_priority_and_refresh_rules(store, settings):
    cand_new = await tok(store, "base", "0x" + "1" * 40, status="candidate")
    cand_ok = await tok(store, "base", "0x" + "2" * 40, status="candidate")
    cand_unknown = await tok(store, "base", "0x" + "3" * 40, status="candidate")
    act_stale = await tok(store, "base", "0x" + "4" * 40)
    act_fresh = await tok(store, "base", "0x" + "5" * 40)
    wl = await tok(store, "base", "0x" + "6" * 40)
    await store.watchlist_upsert(wl, "2026-10-13T00:00:00Z", "F4", 1, 1.0, {})
    old, recent = "2026-10-13T10:00:00Z", "2026-10-14T06:00:00Z"
    await store.add_observation(cand_ok, "enrich", old, {"goplus_status": "ok"})
    await store.add_observation(cand_unknown, "enrich", old, {"goplus_status": "unknown"})
    await store.add_observation(act_stale, "enrich", old, {"goplus_status": "ok"})
    await store.add_observation(act_fresh, "enrich", recent, {"goplus_status": "ok"})
    q = [t for t, _, _ in await enrich_queue(store, settings, NOW)]
    assert q == [wl, act_stale, cand_new, cand_unknown]


async def test_queue_skips_unconfigured_chain_and_benchmark(store, settings):
    settings.chains = ["solana"]
    await tok(store, "base", A)
    await store.upsert_token("benchmark:btc", "benchmark", "btc", "BTC", "Bitcoin", NOW, "coingecko")
    assert await enrich_queue(store, settings, NOW) == []


async def test_run_cap_and_daily_cap(store, settings):
    settings.gp_max_per_run = 1
    await tok(store, "base", A)
    await tok(store, "base", B)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: evm()}})
            r = await enrich(store, settings, s, NOW, sleep=_nosleep)
    assert r.credits_used == 1
    await store.record_run("enrich", NOW, NOW, True, credits_used=999)
    assert await calls_today(store, NOW) == 999
    settings.gp_daily_cap = 999
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            r = await enrich(store, settings, s, NOW, sleep=_nosleep, budget_now=NOW)
            assert not m.requests
    assert r.credits_used == 0


async def test_error_code_counts_failure_and_other_tokens_continue(store, settings):
    await tok(store, "base", A)
    tb = await tok(store, "base", B)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 4029, "message": "rate"})
            m.get(BASE_URL + B, payload={"code": 1, "result": {B: evm()}})
            r = await enrich(store, settings, s, NOW, sleep=_nosleep)
    assert r.rows_written == 1 and len(await enrich_rows(store, tb)) == 1


async def test_every_fetch_failing_raises_partial_failure(store, settings):
    from gecko.runs import PartialFailure

    await tok(store, "base", A)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, status=400)
            with pytest.raises(PartialFailure) as ei:
                await enrich(store, settings, s, NOW, sleep=_nosleep)
    assert ei.value.credits_used == 1


async def test_solana_uses_solana_endpoint(store, settings):
    tid = await tok(store, "solana", WIF)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(SOL_URL + WIF, payload={"code": 1, "result": {WIF: sol()}})
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    (o,) = await enrich_rows(store, tid)
    assert o.features["top10_pct"] == pytest.approx(23.39)


async def test_enrich_feeds_features(store, settings):
    from gecko.features import compute_features

    tid = await tok(store, "base", A)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BASE_URL + A, payload={"code": 1, "result": {A: evm()}})
            await enrich(store, settings, s, NOW, sleep=_nosleep)
    f = await compute_features(store, settings, tid, NOW)
    assert f.security_ok is True and f.holders == 1182384
    assert f.top10_pct == pytest.approx(10.92) and f.bundle_flag is False


async def _nosleep(_):
    return None
```

Append to `tests/gecko/test_gecko_freshness.py`:

```python
async def test_enrich_source_slo(store):
    await store.record_run("enrich", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", True)
    await store.add_observation("base:x", "enrich", "2026-10-01T00:00:00Z", {"goplus_status": "ok"})
    late = "2026-10-02T03:00:00Z"  # 27 h later
    assert "observations:enrich" in {b.table for b in await check_freshness(store, late)}
    assert "observations:enrich" not in {
        b.table for b in await check_freshness(store, "2026-10-01T20:00:00Z")
    }
```

(`check_freshness` is already imported at the top of that file; if not, add `from gecko.freshness import check_freshness`.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_goplus.py tests/gecko/test_gecko_freshness.py -q`
Expected: FAIL with `ImportError: cannot import name 'calls_today'` (and the freshness test failing on a missing key).

- [ ] **Step 3: Implement**

`gecko/settings.py` — add after `cg_max_lookups_per_run`:

```python
    gp_min_interval_sec: float = 2.0  # keyless GoPlus: <=30 calls/min, well under 150 CU/min
    gp_max_per_run: int = 40  # tokens enriched per hourly run
    gp_daily_cap: int = 1000  # calls/day; calibrate from the GoPlus dashboard in the soak
    gp_refresh_hours: int = 20  # active tokens re-read about daily (holder growth needs it)
```

`gecko/store.py` — add after `set_token_status`:

```python
    async def set_deployer(self, token_id, deployer) -> None:
        """Record a token's deployer; the first value recorded is kept."""
        await self.conn.execute(
            "UPDATE tokens SET deployer=COALESCE(deployer, ?), updated_at=? WHERE token_id=?",
            (deployer, utcnow(), token_id),
        )
        await self.conn.commit()
```

`gecko/freshness.py` — add `"observations:enrich": 1560,` to `SLO_MINUTES` (after the OHLCV line) and `"observations:enrich": ("enrich",),` to `WRITER_JOBS`.

`gecko/collectors/goplus.py` — append:

```python
import asyncio
from datetime import timedelta

import aiohttp
import structlog

from gecko.http import Pacer, UpstreamError, get_json
from gecko.jobs import register
from gecko.runs import JobResult, PartialFailure, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow

log = structlog.get_logger("gecko.goplus")
UNKNOWN = {"goplus_status": "unknown"}


async def calls_today(store: Store, now: str) -> int:
    day = canon_ts(now)[:10] + "T00:00:00Z"
    row = await (
        await store.conn.execute(
            "SELECT COALESCE(SUM(credits_used),0) FROM runs WHERE job='enrich' AND started_at>=?",
            (day,),
        )
    ).fetchone()
    return int(row[0])


async def enrich_queue(store: Store, settings: GeckoSettings, now: str) -> list[tuple[str, str, str]]:
    stale = canon_ts((parse_utc(now) - timedelta(hours=settings.gp_refresh_hours)).isoformat())
    q = ",".join("?" * len(settings.chains))
    rows = await (
        await store.conn.execute(
            "SELECT t.token_id, t.chain, t.address, t.status, w.token_id IS NOT NULL, "
            "(SELECT MAX(observed_at) FROM observations o WHERE o.token_id=t.token_id "
            " AND o.source='enrich'), "
            "(SELECT COUNT(*) FROM observations o WHERE o.token_id=t.token_id "
            " AND o.source='enrich' AND json_extract(o.features_json,'$.goplus_status')='ok') "
            "FROM tokens t LEFT JOIN watchlist w ON w.token_id=t.token_id AND w.exited_at IS NULL "
            f"WHERE t.status IN ('candidate','active') AND t.chain IN ({q})",
            tuple(settings.chains),
        )
    ).fetchall()
    due = [
        r for r in rows
        if r[5] is None or (r[5] < stale and (r[3] == "active" or r[6] == 0))
    ]
    due.sort(key=lambda r: (not r[4], r[3] != "active", r[5] is not None, r[5] or "", r[0]))
    return [(r[0], r[1], r[2]) for r in due]


async def _fetch(session, settings, chain, address):
    if chain in CHAIN_IDS:
        url = GOPLUS_EVM_URL.format(chain_id=CHAIN_IDS[chain])
    else:
        url = GOPLUS_SOL_URL
    body = await get_json(session, url, settings=settings, params={"contract_addresses": address})
    return pick_result(chain, address, body)


async def enrich(
    store: Store, settings: GeckoSettings, session: aiohttp.ClientSession, now: str, *,
    sleep=asyncio.sleep, budget_now: str | None = None,
) -> JobResult:
    # The budget day is always the real one (budget_now is a test seam), same
    # ruling as the CoinGecko cap: an --as-of replay spends real calls today.
    budget = max(0, settings.gp_daily_cap - await calls_today(store, budget_now or utcnow()))
    todo = (await enrich_queue(store, settings, now))[: min(settings.gp_max_per_run, budget)]
    pacer = Pacer(settings.gp_min_interval_sec, sleep)
    calls = written = failed = disq = 0
    for tid, chain, address in todo:
        await pacer.wait()
        calls += 1
        try:
            r = await _fetch(session, settings, chain, address)
        except (UpstreamError, GoPlusError) as exc:
            failed += 1
            log.warning("goplus_fetch_failed", token_id=tid, error=type(exc).__name__,
                        status=getattr(exc, "status", None), code=getattr(exc, "code", None))
            continue
        except ValueError as exc:  # non-JSON body
            failed += 1
            log.warning("goplus_body_invalid", token_id=tid, error=type(exc).__name__)
            continue
        if r is None:
            await store.add_observation(tid, "enrich", now, dict(UNKNOWN))
            written += 1
            continue
        f = goplus_features(chain, r)
        deployer = f.pop("deployer")
        if deployer:
            await store.set_deployer(tid, deployer)
        await store.add_observation(tid, "enrich", now, f)
        written += 1
        if f["security_ok"] is False:
            await store.set_token_status(tid, "disqualified", "security_fail")
            disq += 1
    log.info("enrich_done", planned=len(todo), written=written, failed=failed,
             disqualified=disq, budget_left=budget - calls)
    if todo and failed == len(todo):
        raise PartialFailure(RuntimeError("every GoPlus fetch failed"), credits_used=calls)
    return JobResult(rows_written=written, credits_used=calls)


@register("enrich")
async def enrich_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        return await enrich(store, settings, session, now)

    return await run_job("enrich", settings, body)
```

Move the new imports to the top of the module with the existing ones (one import block). `gecko/__main__.py::_load_jobs` — add `import gecko.collectors.goplus  # noqa: F401`.

Note: `aioresponses` matches the URL including the query string, which is why the tests mock `BASE_URL + A`; `get_json` passes `params`, and aiohttp builds the identical URL.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko -q`
Expected: PASS (whole gecko suite; `test_gecko_cli.py` still lists jobs).

- [ ] **Step 5: Commit**

```bash
git add gecko/ tests/gecko/
git commit -m "feat(gecko): enrich job — keyless GoPlus, daily cap, security_fail disqualification, enrich SLO"
```

---

### Task 3: Calls — parse, insert, resolve, seed

**Files:**
- Create: `gecko/calls.py`
- Modify: `gecko/settings.py`
- Test: `tests/gecko/test_gecko_calls.py`

**Interfaces:**
- Consumes: `gecko.identity` (`EVM_CHAINS`, `is_valid_address`, `norm_address`); `Store`, `canon_ts`, `parse_utc`.
- Produces:
  - setting `tg_seed_max: int = 60`
  - `parse_call(text: str | None) -> tuple[list[str], list[str]]` — (cashtags uppercased without `$`, sorted, deduped; valid addresses in first-seen order, deduped, EVM lowercased)
  - `event_id(handle: str, msg_id: int) -> str` → `"tg:<handle lowercased>:<msg_id>"`
  - `async insert_call(store, *, source_type: str, source_id: str, event_id: str, posted_at: str, text: str | None) -> bool` — True when a NEW row was written; messages with neither cashtag nor address are not stored
  - `async resolve_calls(store, now: str, *, since: str | None) -> int` — rows resolved
  - `async call_seeds(store, settings, now: str) -> dict[str, set[str]]` — chain → normalized addresses, from unresolved calls in the last 24 h

- [ ] **Step 1: Write the failing tests**

```python
# tests/gecko/test_gecko_calls.py
import json

from gecko.calls import call_seeds, event_id, insert_call, parse_call, resolve_calls

NOW = "2026-10-14T12:00:00Z"
EVM = "0x" + "Ab" * 20
SOL = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


def test_parse_cashtags_and_addresses():
    text = f"$wif $WIF $100 aped {SOL} and {EVM} https://dexscreener.com/base/{EVM}"
    tags, addrs = parse_call(text)
    assert tags == ["WIF"]
    assert addrs == [SOL, EVM.lower()]  # message order; the URL repeat is deduped


def test_parse_ignores_long_base58_and_empty():
    sig = "5" * 88  # a transaction signature is not a mint
    assert parse_call(f"tx {sig}") == ([], [])
    assert parse_call(None) == ([], [])
    assert parse_call("") == ([], [])


def test_event_id_is_lowercased_handle():
    assert event_id("@Gem_Detecter", 42) == "tg:@gem_detecter:42"


async def test_insert_is_idempotent_and_skips_noise(store):
    kw = dict(source_type="telegram", source_id="@c", posted_at="2026-10-14T11:00:00+00:00")
    assert await insert_call(store, event_id="tg:@c:1", text=f"buy {SOL}", **kw) is True
    assert await insert_call(store, event_id="tg:@c:1", text=f"buy {SOL}", **kw) is False
    assert await insert_call(store, event_id="tg:@c:2", text="gm frens", **kw) is False
    row = await (await store.conn.execute(
        "SELECT posted_at, addresses_json, cashtags_json, token_id FROM source_calls")).fetchone()
    assert row == ("2026-10-14T11:00:00Z", json.dumps([SOL]), "[]", None)


async def _token(store, chain, addr):
    await store.upsert_token(f"{chain}:{addr}", chain, addr, "T", "T", NOW, "dexscreener")


async def test_resolve_unique_match_only(store):
    kw = dict(source_type="telegram", source_id="@c", posted_at="2026-10-14T11:00:00Z")
    await insert_call(store, event_id="tg:@c:1", text=SOL, **kw)
    await insert_call(store, event_id="tg:@c:2", text=EVM, **kw)
    await _token(store, "solana", SOL)
    await _token(store, "base", EVM.lower())
    await _token(store, "bsc", EVM.lower())  # same address on two EVM chains: ambiguous
    assert await resolve_calls(store, NOW, since="2026-10-07T00:00:00Z") == 1
    rows = await (await store.conn.execute(
        "SELECT event_id, token_id, resolved_at FROM source_calls ORDER BY event_id")).fetchall()
    assert rows == [("tg:@c:1", f"solana:{SOL}", NOW), ("tg:@c:2", None, None)]


async def test_resolve_respects_window(store):
    await insert_call(store, source_type="telegram", source_id="@c", event_id="tg:@c:1",
                      posted_at="2026-09-01T00:00:00Z", text=SOL)
    await _token(store, "solana", SOL)
    assert await resolve_calls(store, NOW, since="2026-10-07T00:00:00Z") == 0
    assert await resolve_calls(store, NOW, since=None) == 1


async def test_seeds_expand_evm_over_configured_chains(store, settings):
    settings.chains = ["solana", "base", "bsc"]
    kw = dict(source_type="telegram", source_id="@c")
    await insert_call(store, event_id="tg:@c:1", posted_at="2026-10-14T11:00:00Z", text=f"{SOL} {EVM}", **kw)
    await insert_call(store, event_id="tg:@c:2", posted_at="2026-10-12T00:00:00Z", text="0x" + "c" * 40, **kw)
    seeds = await call_seeds(store, settings, NOW)
    assert seeds == {"solana": {SOL}, "base": {EVM.lower()}, "bsc": {EVM.lower()}}


async def test_seeds_skip_resolved_and_respect_cap(store, settings):
    settings.tg_seed_max = 1
    kw = dict(source_type="telegram", source_id="@c", posted_at="2026-10-14T11:00:00Z")
    await insert_call(store, event_id="tg:@c:1", text=SOL, **kw)
    await insert_call(store, event_id="tg:@c:2", text="0x" + "d" * 40, **kw)
    seeds = await call_seeds(store, settings, NOW)
    assert sum(len(v) for v in seeds.values()) <= len(settings.chains)  # one address, expanded
    await _token(store, "solana", SOL)
    await resolve_calls(store, NOW, since=None)
    assert SOL not in (await call_seeds(store, settings, NOW)).get("solana", set())
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_calls.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'gecko.calls'`.

- [ ] **Step 3: Implement**

`gecko/settings.py` — add `tg_seed_max: int = 60  # called addresses offered to DexScreener per run`.

```python
# gecko/calls.py
"""Social calls: parse a message, store it once, map it to a known token.

Only contract addresses resolve a call. Cashtags are stored for later
analysis but never resolve (a symbol names many tokens).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

from gecko.identity import EVM_CHAINS, is_valid_address, norm_address
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc

_CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{1,11})\b")
_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
SEED_HOURS = 24


def parse_call(text: str | None) -> tuple[list[str], list[str]]:
    if not text:
        return [], []
    tags = sorted({m.group(1).upper() for m in _CASHTAG.finditer(text)})
    found: list[tuple[int, str]] = []
    for m in _EVM.finditer(text):
        found.append((m.start(), m.group(0).lower()))
    for m in _SOL.finditer(text):
        if is_valid_address("solana", m.group(0)):
            found.append((m.start(), m.group(0)))
    addrs: list[str] = []
    for _, a in sorted(found):
        if a not in addrs:
            addrs.append(a)
    return tags, addrs


def event_id(handle: str, msg_id: int) -> str:
    return f"tg:{handle.lower()}:{msg_id}"


async def insert_call(store: Store, *, source_type, source_id, event_id, posted_at, text) -> bool:
    tags, addrs = parse_call(text)
    if not tags and not addrs:
        return False
    cur = await store.conn.execute(
        "INSERT OR IGNORE INTO source_calls(source_type,source_id,event_id,posted_at,"
        "text_hash,cashtags_json,addresses_json) VALUES(?,?,?,?,?,?,?)",
        (source_type, source_id, event_id, canon_ts(posted_at),
         hashlib.sha256((text or "").encode()).hexdigest(), json.dumps(tags), json.dumps(addrs)),
    )
    await store.conn.commit()
    return cur.rowcount == 1


async def _matches(store: Store, addr: str) -> set[str]:
    if addr.startswith("0x"):
        q = ",".join("?" * len(EVM_CHAINS))
        sql, args = f"SELECT token_id FROM tokens WHERE address=? AND chain IN ({q})", (addr, *sorted(EVM_CHAINS))
    else:
        sql, args = "SELECT token_id FROM tokens WHERE address=? AND chain='solana'", (addr,)
    return {r[0] for r in await (await store.conn.execute(sql, args)).fetchall()}


async def resolve_calls(store: Store, now: str, *, since: str | None) -> int:
    sql = "SELECT id, addresses_json FROM source_calls WHERE token_id IS NULL AND addresses_json!='[]'"
    args: tuple = ()
    if since is not None:
        sql += " AND posted_at>=?"
        args = (canon_ts(since),)
    resolved = 0
    for cid, aj in await (await store.conn.execute(sql, args)).fetchall():
        tids: set[str] = set()
        for a in json.loads(aj):
            tids |= await _matches(store, a)
        if len(tids) != 1:
            continue  # unknown yet, or ambiguous across chains
        cur = await store.conn.execute(
            "UPDATE source_calls SET token_id=?, resolved_at=? WHERE id=? AND token_id IS NULL",
            (tids.pop(), canon_ts(now), cid),
        )
        resolved += cur.rowcount
    await store.conn.commit()
    return resolved


async def call_seeds(store: Store, settings: GeckoSettings, now: str) -> dict[str, set[str]]:
    since = canon_ts((parse_utc(now) - timedelta(hours=SEED_HOURS)).isoformat())
    rows = await (
        await store.conn.execute(
            "SELECT addresses_json FROM source_calls WHERE token_id IS NULL "
            "AND addresses_json!='[]' AND posted_at>=? AND posted_at<=? ORDER BY posted_at DESC",
            (since, canon_ts(now)),
        )
    ).fetchall()
    addrs: list[str] = []
    for (aj,) in rows:
        for a in json.loads(aj):
            if a not in addrs:
                addrs.append(a)
    seeds: dict[str, set[str]] = {}
    for a in addrs[: settings.tg_seed_max]:
        chains = [c for c in settings.chains if c in EVM_CHAINS] if a.startswith("0x") else (
            ["solana"] if "solana" in settings.chains else [])
        for c in chains:
            seeds.setdefault(c, set()).add(norm_address(c, a))
    return seeds
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko/test_gecko_calls.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gecko/calls.py gecko/settings.py tests/gecko/test_gecko_calls.py
git commit -m "feat(gecko): call parsing, idempotent insert, address-only resolution, discovery seeds"
```

---

### Task 4: Telegram calls as a DexScreener discovery feed

**Files:**
- Modify: `gecko/collectors/dexscreener.py` (`collect`)
- Test: `tests/gecko/test_gecko_dexscreener.py`

**Interfaces:**
- Consumes: `gecko.calls.call_seeds(store, settings, now)`.
- Produces: tokens first discovered via a seed (and not in the boosts/profiles feeds this run) get `tokens.first_source = "telegram"`; seeds pass through the same `in_band` filter as feed addresses.

- [ ] **Step 1: Write the failing tests**

Append to `tests/gecko/test_gecko_dexscreener.py` (reuses `pair`, `collect`, URL constants, `NOW` already in the file):

```python
from gecko.calls import insert_call


async def test_called_address_is_discovered_with_telegram_source(store, settings):
    addr = "0xabc0000000000000000000000000000000000000"
    settings.chains = ["base"]
    await insert_call(store, source_type="telegram", source_id="@c", event_id="tg:@c:1",
                      posted_at="2026-10-08T23:00:00Z", text=f"ape {addr}")
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BOOSTS_URL, payload=[])
            m.get(PROFILES_URL, payload=[])
            m.get(TOKENS_URL.format(chain="base", addresses=addr), payload=[pair(addr=addr)])
            await collect(store, settings, s, NOW, sleep=_nosleep)
    row = await (await store.conn.execute(
        "SELECT first_source FROM tokens WHERE token_id=?", (f"base:{addr}",))).fetchone()
    assert row == ("telegram",)


async def test_seed_also_in_feed_keeps_dexscreener_source(store, settings):
    addr = "0xabc0000000000000000000000000000000000000"
    settings.chains = ["base"]
    await insert_call(store, source_type="telegram", source_id="@c", event_id="tg:@c:1",
                      posted_at="2026-10-08T23:00:00Z", text=addr)
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BOOSTS_URL, payload=[{"chainId": "base", "tokenAddress": addr}])
            m.get(PROFILES_URL, payload=[])
            m.get(TOKENS_URL.format(chain="base", addresses=addr), payload=[pair(addr=addr)])
            await collect(store, settings, s, NOW, sleep=_nosleep)
    row = await (await store.conn.execute(
        "SELECT first_source FROM tokens WHERE token_id=?", (f"base:{addr}",))).fetchone()
    assert row == ("dexscreener",)
```

If the file has no `_nosleep` helper, add `async def _nosleep(_): return None` (check the existing tests' sleep seam name first and reuse it).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_dexscreener.py -q -k "telegram or seed"`
Expected: FAIL — the first test finds no token row (the seed is never fetched).

- [ ] **Step 3: Implement**

In `gecko/collectors/dexscreener.py` add `from gecko.calls import call_seeds` and change the start of `collect` after `_discover`:

```python
    found, invalid = await _discover(session, settings, pacer, feeds)
    seeded: set[tuple[str, str]] = set()
    for chain, addrs in (await call_seeds(store, settings, now)).items():
        have = found.setdefault(chain, set())
        seeded |= {(chain, a) for a in addrs - have}
        have |= addrs
```

and in the in-band branch replace the hard-coded `"dexscreener"` first-source argument of `store.upsert_token(...)` with:

```python
                "telegram" if (chain, addr) in seeded else "dexscreener",
```

(the `add_observation(..., "dexscreener", ...)` source stays `"dexscreener"` — the observation's source is the data provider, not the discoverer).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko/test_gecko_dexscreener.py -q`
Expected: PASS (existing tests unaffected: no calls → empty seeds).

- [ ] **Step 5: Commit**

```bash
git add gecko/collectors/dexscreener.py tests/gecko/test_gecko_dexscreener.py
git commit -m "feat(gecko): unresolved Telegram-called addresses join DexScreener discovery"
```

---

### Task 5: Telegram reader (`telegram-reader`, `tg-login`)

**Files:**
- Create: `gecko/telegram_reader.py`
- Modify: `gecko/settings.py`, `gecko/freshness.py`, `gecko/__main__.py`
- Test: `tests/gecko/test_gecko_telegram.py`, `tests/gecko/test_gecko_freshness.py`

**Interfaces:**
- Consumes: Task 3 (`insert_call`, `resolve_calls`, `event_id`); `Store.record_run`, `utcnow`, `canon_ts`.
- Produces:
  - settings `tg_api_id: int = 0`, `tg_api_hash: SecretStr | None = None`, `tg_session_path: str = ""`, `tg_channels: list[str] = []`, `tg_poll_interval_sec: int = 120`, `tg_catchup_limit: int = 200`
  - `session_problem(settings, *, must_exist: bool) -> str | None`
  - `async initial_marks(store, handles: list[str]) -> dict[str, int]`
  - `async poll_once(store, client, settings, entities: dict[str, object], marks: dict[str, int], *, now: str | None = None) -> tuple[int, int]` — (rows written, channels failed); `now` (default: real clock) anchors the 7-day resolution window
  - `async run_reader(store, client, settings, *, sleep=asyncio.sleep, cycles: int | None = None) -> None`
  - jobs `telegram-reader` (long-running) and `tg-login` (interactive, operator-run once)
  - `runs` rows: `job="telegram-reader"`, one per poll, `ok = (failed == 0)`
  - freshness: `source_calls` run-anchored, SLO 30 min

`client` is duck-typed so tests use a fake: `await client.get_input_entity(handle)`, `await client.get_messages(entity, limit=int, min_id=int)` returning message objects with `.id: int`, `.date: datetime (aware UTC)`, `.message: str | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/gecko/test_gecko_telegram.py
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from gecko.telegram_reader import initial_marks, poll_once, run_reader, session_problem

SOL = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


def msg(i, text, day=14):
    return SimpleNamespace(id=i, date=datetime(2026, 10, day, 11, i % 60, tzinfo=timezone.utc), message=text)


class FakeClient:
    def __init__(self, chans: dict[str, list], fail: set[str] = frozenset()):
        self.chans, self.fail = chans, set(fail)
        self.resolved: list[str] = []
        self.requests: list[tuple[str, int, int]] = []

    async def get_input_entity(self, handle):
        self.resolved.append(handle)
        if handle.lower() not in self.chans:
            raise ValueError(f"No user has {handle} as username")
        return handle.lower()

    async def get_messages(self, entity, limit, min_id):
        self.requests.append((entity, limit, min_id))
        if entity in self.fail:
            raise ConnectionError("boom")
        ms = [m for m in self.chans[entity] if m.id > min_id]
        return sorted(ms, key=lambda m: -m.id)[:limit]


async def _runs(store):
    return await (await store.conn.execute(
        "SELECT ok, rows_written, error FROM runs WHERE job='telegram-reader' ORDER BY id")).fetchall()


async def test_poll_ingests_calls_and_advances_marks(store, settings):
    c = FakeClient({"@a": [msg(1, f"ape {SOL}"), msg(2, "gm"), msg(3, "$WIF")]})
    marks = {}
    written, failed = await poll_once(store, c, settings, {"@a": "@a"}, marks)
    assert (written, failed) == (2, 0) and marks == {"@a": 3}
    await poll_once(store, c, settings, {"@a": "@a"}, marks)
    assert c.requests[-1] == ("@a", settings.tg_catchup_limit, 3)


async def test_failed_channel_marks_run_not_ok(store, settings):
    settings.tg_channels = ["@a", "@b"]
    c = FakeClient({"@a": [msg(1, SOL)], "@b": []}, fail={"@b"})
    await run_reader(store, c, settings, cycles=1)
    (ok, rows, err), = await _runs(store)
    assert ok == 0 and rows == 1 and "1 channel" in err


async def test_entities_resolved_once(store, settings):
    settings.tg_channels = ["@A"]
    c = FakeClient({"@a": [msg(1, SOL)]})
    await run_reader(store, c, settings, cycles=3, sleep=_nosleep)
    assert c.resolved == ["@A"]
    assert [r[0] for r in await _runs(store)] == [1, 1, 1]


async def test_unresolvable_channel_fails_fast(store, settings):
    settings.tg_channels = ["@missing"]
    with pytest.raises(ValueError):
        await run_reader(store, FakeClient({}), settings, cycles=1)


async def test_restart_resumes_from_stored_calls(store, settings):
    c = FakeClient({"@a": [msg(5, SOL), msg(9, "$ABC")], "@a_b": [msg(4, SOL)]})
    await poll_once(store, c, settings, {"@a": "@a", "@a_b": "@a_b"}, {})
    # "@a_b" must not match "@a" (an "_" is a LIKE wildcard; the query must not use LIKE)
    assert await initial_marks(store, ["@a", "@a_b", "@b"]) == {"@a": 9, "@a_b": 4, "@b": 0}
    assert await initial_marks(store, ["@aXb"]) == {"@aXb": 0}


async def test_poll_resolves_calls_to_known_tokens(store, settings):
    await store.upsert_token(f"solana:{SOL}", "solana", SOL, "WIF", "wif", "2026-10-01T00:00:00Z", "dexscreener")
    c = FakeClient({"@a": [msg(1, SOL)]})
    await poll_once(store, c, settings, {"@a": "@a"}, {}, now="2026-10-14T12:00:00Z")
    row = await (await store.conn.execute("SELECT token_id FROM source_calls")).fetchone()
    assert row == (f"solana:{SOL}",)


def test_session_problems(tmp_path, settings):
    from pydantic import SecretStr

    settings.tg_api_id, settings.tg_api_hash = 1, SecretStr("h")
    settings.tg_channels = ["@a"]
    ok = tmp_path / "gk.session"
    settings.tg_session_path = str(ok)
    assert "missing" in session_problem(settings, must_exist=True)
    assert session_problem(settings, must_exist=False) is None
    ok.write_text("x")
    os.chmod(ok, 0o600)
    assert session_problem(settings, must_exist=True) is None
    settings.tg_session_path = "rel.session"
    assert "absolute" in session_problem(settings, must_exist=False)
    from pathlib import Path

    settings.tg_session_path = str(Path(__file__).resolve().parents[2] / "x.session")
    assert "inside the repo" in session_problem(settings, must_exist=False)
    settings.tg_session_path = str(tmp_path / "gk.txt")
    assert ".session" in session_problem(settings, must_exist=False)
    settings.tg_session_path = str(ok)
    settings.tg_channels = []
    assert "GECKO_TG_CHANNELS" in session_problem(settings, must_exist=True)
    settings.tg_channels, settings.tg_api_id = ["@a"], 0
    assert "API" in session_problem(settings, must_exist=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_session_readable_by_others_refused(tmp_path, settings):
    from pydantic import SecretStr

    settings.tg_api_id, settings.tg_api_hash, settings.tg_channels = 1, SecretStr("h"), ["@a"]
    p = tmp_path / "gk.session"
    p.write_text("x")
    os.chmod(p, 0o644)
    settings.tg_session_path = str(p)
    assert "0600" in session_problem(settings, must_exist=True)


async def _nosleep(_):
    return None
```

Append to `tests/gecko/test_gecko_freshness.py`:

```python
async def test_source_calls_is_run_anchored_on_the_reader(store):
    # silent channels: reader polls fine, writes no calls -> no page
    await store.record_run("telegram-reader", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", True)
    await store.record_run("telegram-reader", "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z", True)
    assert "source_calls" not in {b.table for b in await check_freshness(store, "2026-10-02T00:20:00Z")}
    # reader stopped polling (or every poll failed) -> page after 30 min
    await store.record_run("telegram-reader", "2026-10-02T00:02:00Z", "2026-10-02T00:02:00Z", False)
    assert "source_calls" in {b.table for b in await check_freshness(store, "2026-10-02T00:40:00Z")}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_telegram.py tests/gecko/test_gecko_freshness.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'gecko.telegram_reader'`.

- [ ] **Step 3: Implement**

`gecko/settings.py` — add:

```python
    tg_api_id: int = 0
    tg_api_hash: SecretStr | None = None
    tg_session_path: str = ""  # absolute, outside the repo, mode 0600; gecko's OWN session
    tg_channels: list[str] = []  # public @handles
    tg_poll_interval_sec: int = 120
    tg_catchup_limit: int = 200  # newest messages read per channel per poll
```

`gecko/freshness.py`: `"source_calls": 30,` in `SLO_MINUTES` (was 360) and `RUN_ANCHORED = frozenset({"watchlist", "forward_returns", "research_notes", "source_calls"})`. Update the comment above `RUN_ANCHORED`: "Tables whose writer may legitimately write 0 rows (silent channels, an empty watchlist, nothing due); judged by its last ok run."

```python
# gecko/telegram_reader.py
"""Telegram caller channels: a long-running poll loop over gecko's own session.

Each poll reads messages newer than the last one seen per channel, stores the
ones carrying a cashtag or contract address, resolves them to known tokens,
and writes one ``runs`` row (ok only if every channel was read), so a reader
that cannot poll pages through freshness even when the channels are silent.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path

import structlog

from gecko.calls import event_id, insert_call, resolve_calls
from gecko.jobs import register
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow

log = structlog.get_logger("gecko.telegram")
REPO = Path(__file__).resolve().parents[1]
RESOLVE_DAYS = 7


def session_problem(settings: GeckoSettings, *, must_exist: bool) -> str | None:
    if settings.tg_api_id <= 0 or settings.tg_api_hash is None:
        return "GECKO_TG_API_ID / GECKO_TG_API_HASH not set"
    if not settings.tg_channels:
        return "GECKO_TG_CHANNELS is empty"
    p = Path(settings.tg_session_path)
    if not p.is_absolute():
        return f"GECKO_TG_SESSION_PATH must be absolute (got {settings.tg_session_path!r})"
    if p.suffix != ".session":
        return "GECKO_TG_SESSION_PATH must end in .session"
    if REPO in p.resolve().parents:
        return "GECKO_TG_SESSION_PATH is inside the repo; a session is an identity secret"
    if must_exist:
        if not p.exists():
            return f"session file missing at {p}; run `python -m gecko tg-login`"
        if os.name == "posix" and stat.S_IMODE(p.stat().st_mode) & 0o077:
            return f"session file {p} must be mode 0600"
    return None


async def initial_marks(store: Store, handles: list[str]) -> dict[str, int]:
    marks: dict[str, int] = {}
    for h in handles:
        prefix = event_id(h, 0)[:-1]
        # substr equality, not LIKE: handles contain "_", a LIKE wildcard.
        row = await (
            await store.conn.execute(
                "SELECT MAX(CAST(substr(event_id, ?) AS INTEGER)) FROM source_calls "
                "WHERE source_type='telegram' AND substr(event_id, 1, ?)=?",
                (len(prefix) + 1, len(prefix), prefix),
            )
        ).fetchone()
        marks[h] = row[0] or 0
    return marks


async def poll_once(store, client, settings, entities, marks, *, now: str | None = None) -> tuple[int, int]:
    written = failed = 0
    for handle, ent in entities.items():
        try:
            msgs = await client.get_messages(ent, limit=settings.tg_catchup_limit, min_id=marks.get(handle, 0))
        except Exception as exc:  # noqa: BLE001 — RPC/flood/transport: recorded, other channels go on
            failed += 1
            log.warning("tg_poll_failed", channel=handle, error=type(exc).__name__,
                        wait_sec=getattr(exc, "seconds", None))
            continue
        for m in sorted(msgs, key=lambda m: m.id):
            marks[handle] = max(marks.get(handle, 0), m.id)
            if await insert_call(store, source_type="telegram", source_id=handle,
                                 event_id=event_id(handle, m.id),
                                 posted_at=m.date.isoformat(), text=m.message):
                written += 1
    now = canon_ts(now) or utcnow()  # test seam: a fixed clock, no date bomb
    since = canon_ts((parse_utc(now) - timedelta(days=RESOLVE_DAYS)).isoformat())
    await resolve_calls(store, now, since=since)
    return written, failed


async def run_reader(store, client, settings, *, sleep=asyncio.sleep, cycles=None) -> None:
    entities = {h.lower(): await client.get_input_entity(h) for h in settings.tg_channels}
    marks = await initial_marks(store, list(entities))
    log.info("tg_reader_started", channels=len(entities), marks=marks)
    n = 0
    while cycles is None or n < cycles:
        started = utcnow()
        written, failed = await poll_once(store, client, settings, entities, marks)
        await store.record_run("telegram-reader", started, utcnow(), failed == 0, rows_written=written,
                               error=f"{failed} channel(s) failed" if failed else None)
        n += 1
        if cycles is None or n < cycles:
            await sleep(settings.tg_poll_interval_sec)


def _client(settings: GeckoSettings):
    from telethon import TelegramClient  # lazy: every other job stays Telethon-free

    return TelegramClient(settings.tg_session_path.removesuffix(".session"),
                          settings.tg_api_id, settings.tg_api_hash.get_secret_value())


@register("telegram-reader")
async def telegram_reader_job(settings: GeckoSettings, ns) -> int:
    if problem := session_problem(settings, must_exist=True):
        print(problem, file=sys.stderr)
        return 2
    client = _client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("telegram session not authorized; run `python -m gecko tg-login`", file=sys.stderr)
            return 1
        store = await Store.open(settings.db_path)
        try:
            await run_reader(store, client, settings)
        finally:
            await store.close()
    finally:
        await client.disconnect()
    return 0


@register("tg-login")
async def tg_login_job(settings: GeckoSettings, ns) -> int:
    """Interactive, operator-run once: creates gecko's own session (a new device)."""
    if problem := session_problem(settings, must_exist=False):
        print(problem, file=sys.stderr)
        return 2
    client = _client(settings)
    await client.start()  # prompts for phone and login code on this terminal
    await client.disconnect()
    os.chmod(settings.tg_session_path, 0o600)
    print(f"session written to {settings.tg_session_path} (0600)")
    return 0
```

`gecko/__main__.py::_load_jobs` — add `import gecko.telegram_reader  # noqa: F401`.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko -q`
Expected: PASS. Then `python -c "import gecko.__main__ as m; m._load_jobs(); import sys; print('telethon' in sys.modules)"` → `False` (Telethon stays lazy).

- [ ] **Step 5: Commit**

```bash
git add gecko/ tests/gecko/
git commit -m "feat(gecko): telegram-reader poll loop on its own session, tg-login, run-anchored source_calls SLO"
```

---

### Task 6: `migrate-calls` — legacy Telegram history into `gecko.db`

**Files:**
- Modify: `gecko/telegram_reader.py`
- Test: `tests/gecko/test_gecko_telegram.py`

**Interfaces:**
- Consumes: `insert_call`, `resolve_calls`, `event_id`; `run_job`; `ns.history_db` (existing CLI arg, default `/root/gecko-history/scout.db`).
- Produces: job `migrate-calls` — reads `tg_social_messages` READ-ONLY, inserts through the same `insert_call` path with `event_id(channel_handle, msg_id)` (so a live reader poll of the same message is a no-op), resolves with `since=None`, and prints the active legacy channel list as a ready `GECKO_TG_CHANNELS=[...]` line. Idempotent: a second run writes 0.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3

from gecko.__main__ import main


def _legacy_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE tg_social_messages(id INTEGER PRIMARY KEY, channel_handle TEXT, msg_id INTEGER,"
               " posted_at TEXT, sender TEXT, text TEXT, cashtags TEXT, contracts TEXT, urls TEXT, parsed_at TEXT)")
    db.execute("CREATE TABLE tg_social_channels(id INTEGER PRIMARY KEY, channel_handle TEXT, display_name TEXT,"
               " trade_eligible INTEGER, safety_required INTEGER, added_at TEXT, removed_at TEXT)")
    db.executemany("INSERT INTO tg_social_messages(channel_handle,msg_id,posted_at,text,parsed_at) VALUES(?,?,?,?,?)",
                   [("@Gem", 7, "2026-08-01T00:00:00", f"ape {SOL}", "x"),
                    ("@Gem", 8, "2026-08-01T00:01:00", "gm", "x")])
    db.executemany("INSERT INTO tg_social_channels(channel_handle,display_name,added_at,removed_at) VALUES(?,?,?,?)",
                   [("@Gem", "g", "x", None), ("@Old", "o", "x", "2026-07-01")])
    db.commit()
    db.close()


def test_migrate_calls_is_idempotent_and_read_only(tmp_path, monkeypatch, capsys):
    legacy = tmp_path / "scout.db"
    _legacy_db(legacy)
    before = legacy.read_bytes()
    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))
    assert main(["migrate-calls", "--history-db", str(legacy)]) == 0
    out = capsys.readouterr().out
    assert "migrated 1 call(s)" in out and 'GECKO_TG_CHANNELS=["@gem"]' in out
    assert main(["migrate-calls", "--history-db", str(legacy)]) == 0
    assert "migrated 0 call(s)" in capsys.readouterr().out
    assert legacy.read_bytes() == before
    db = sqlite3.connect(tmp_path / "gecko.db")
    assert db.execute("SELECT event_id, posted_at FROM source_calls").fetchall() == [
        ("tg:@gem:7", "2026-08-01T00:00:00Z")]


def test_migrate_calls_missing_db_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))
    assert main(["migrate-calls", "--history-db", str(tmp_path / "nope.db")]) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_telegram.py -q -k migrate`
Expected: FAIL — `unknown job: 'migrate-calls'` (exit 2).

- [ ] **Step 3: Implement**

Append to `gecko/telegram_reader.py` (add `import json` and `import aiosqlite` to the imports; import `JobResult, run_job` from `gecko.runs`):

```python
@register("migrate-calls")
async def migrate_calls_job(settings: GeckoSettings, ns) -> int:
    path = Path(ns.history_db)
    if not path.is_file():
        print(f"history db not found: {path}", file=sys.stderr)
        return 1

    async def body(store: Store, session) -> JobResult:
        n = 0
        # mode=ro: the legacy database is evidence, never written or migrated.
        async with aiosqlite.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as legacy:
            async with legacy.execute(
                "SELECT channel_handle, msg_id, posted_at, text FROM tg_social_messages ORDER BY id"
            ) as cur:
                async for handle, msg_id, posted_at, text in cur:
                    n += await insert_call(store, source_type="telegram", source_id=handle.lower(),
                                           event_id=event_id(handle, msg_id), posted_at=posted_at, text=text)
            chans = [r[0].lower() for r in await (await legacy.execute(
                "SELECT channel_handle FROM tg_social_channels WHERE removed_at IS NULL ORDER BY channel_handle"
            )).fetchall()]
        resolved = await resolve_calls(store, utcnow(), since=None)
        print(f"migrated {n} call(s), resolved {resolved}")
        print(f"GECKO_TG_CHANNELS={json.dumps(chans)}")
        return JobResult(rows_written=n)

    return await run_job("migrate-calls", settings, body)
```

Note: `json.dumps(["@gem"])` prints `["@gem"]` — the exact form pydantic-settings parses for a `list[str]` env var.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko/test_gecko_telegram.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gecko/telegram_reader.py tests/gecko/test_gecko_telegram.py
git commit -m "feat(gecko): migrate-calls imports legacy Telegram history read-only and idempotently"
```

---

### Task 7: `prune` job (observations 180 d, runs 90 d)

**Files:**
- Create: `gecko/prune.py`
- Modify: `gecko/settings.py`, `gecko/__main__.py`
- Test: `tests/gecko/test_gecko_prune.py`

**Interfaces:**
- Produces: settings `obs_retention_days: int = 180` and `runs_retention_days: int = 90`, each validated `>= 30`; `async prune(store, settings, now, *, batch: int = 5000) -> tuple[int, int]` (observations deleted, runs deleted); job `prune`.
- Rules: never delete `benchmark:btc` observations (the 180-day horizon reads BTC at `entered_at`); never delete the earliest ok run of any job (freshness bootstrap reads `MIN(finished_at)` of ok runs); delete in batches, committing each, so the hourly writers are not blocked by one long transaction.
- Why 30 is the floor: features read ≤ 16 days back (vol ratio 14 d, holder "then" 7+2 d), admission 7 d, entry price 48 h after entry, OHLCV markers about 14 d.

- [ ] **Step 1: Write the failing tests**

```python
# tests/gecko/test_gecko_prune.py
import pytest
from pydantic import ValidationError

from gecko.prune import prune
from gecko.settings import GeckoSettings

NOW = "2026-10-14T00:00:00Z"
OLD = "2026-03-01T00:00:00Z"  # > 180 d
MID = "2026-06-01T00:00:00Z"  # > 90 d, < 180 d


async def _count(store, sql):
    return (await (await store.conn.execute(sql)).fetchone())[0]


async def test_prunes_old_observations_but_not_recent(store, settings):
    await store.add_observation("base:x", "dexscreener", OLD, {"price_usd": 1})
    await store.add_observation("base:x", "dexscreener", MID, {"price_usd": 2})
    assert await prune(store, settings, NOW, batch=1) == (1, 0)
    assert await _count(store, "SELECT COUNT(*) FROM observations") == 1


async def test_benchmark_rows_survive_prune(store, settings):
    await store.add_observation("benchmark:btc", "coingecko", OLD, {"price_usd": 1})
    assert (await prune(store, settings, NOW))[0] == 0


async def test_runs_keep_earliest_ok_per_job(store, settings):
    await store.record_run("rank", OLD, OLD, False)
    await store.record_run("rank", OLD, OLD, True)   # earliest ok: kept
    await store.record_run("rank", MID, MID, True)   # old: deleted
    await store.record_run("ledger", MID, MID, True)  # earliest ok of ledger: kept
    await store.record_run("rank", NOW, NOW, True)
    assert (await prune(store, settings, NOW))[1] == 2
    rows = await (await store.conn.execute(
        "SELECT job, finished_at, ok FROM runs ORDER BY id")).fetchall()
    assert rows == [("rank", OLD, 1), ("ledger", MID, 1), ("rank", NOW, 1)]


def test_retention_floor_validated():
    with pytest.raises(ValidationError):
        GeckoSettings(obs_retention_days=29, _env_file=None)
    with pytest.raises(ValidationError):
        GeckoSettings(runs_retention_days=10, _env_file=None)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_prune.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'gecko.prune'`.

- [ ] **Step 3: Implement**

`gecko/settings.py` — add the two fields and a validator:

```python
    obs_retention_days: int = 180
    runs_retention_days: int = 90

    @field_validator("obs_retention_days", "runs_retention_days")
    @classmethod
    def _retention_floor(cls, v: int) -> int:
        # features read <=16 d back, admission 7 d, entry price 48 h: 30 d is the floor
        if v < 30:
            raise ValueError("retention must be >= 30 days")
        return v
```

```python
# gecko/prune.py
from __future__ import annotations

from datetime import timedelta

import aiohttp
import structlog

from gecko.jobs import register
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow

log = structlog.get_logger("gecko.prune")
BTC = "benchmark:btc"  # the 180 d horizon reads BTC at entered_at: never pruned


def _ago(now: str, days: int) -> str:
    return canon_ts((parse_utc(now) - timedelta(days=days)).isoformat())


async def _delete_batches(store: Store, table: str, where: str, args: tuple, batch: int) -> int:
    total = 0
    while True:
        cur = await store.conn.execute(
            f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {where} LIMIT ?)",
            (*args, batch),
        )
        await store.conn.commit()  # short transactions: hourly writers are not blocked
        total += cur.rowcount
        if cur.rowcount < batch:
            return total


async def prune(store: Store, settings: GeckoSettings, now: str, *, batch: int = 5000) -> tuple[int, int]:
    obs = await _delete_batches(
        store, "observations", "observed_at<? AND token_id!=?",
        (_ago(now, settings.obs_retention_days), BTC), batch,
    )
    runs = await _delete_batches(
        store, "runs",
        "finished_at<? AND id NOT IN (SELECT MIN(id) FROM runs WHERE ok=1 GROUP BY job)",
        (_ago(now, settings.runs_retention_days),), batch,
    )
    log.info("prune_done", observations=obs, runs=runs)
    return obs, runs


@register("prune")
async def prune_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        obs, runs = await prune(store, settings, now)
        return JobResult(rows_written=obs + runs)

    return await run_job("prune", settings, body)
```

`gecko/__main__.py::_load_jobs` — add `import gecko.prune  # noqa: F401`.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko/test_gecko_prune.py tests/gecko/test_gecko_settings.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gecko/prune.py gecko/settings.py gecko/__main__.py tests/gecko/test_gecko_prune.py
git commit -m "feat(gecko): prune job — observations 180d (BTC kept), runs 90d (earliest ok kept), 30d floor"
```

---

### Task 8: Ledger — CoinGecko fallback only after the deadline; expire stuck rows

**Files:**
- Modify: `gecko/ledger.py` (`entry_price`, `_resolve_entry`, `measure_due`, `LedgerReport`)
- Modify: `gecko/store.py` (`close_unmeasured`), `gecko/settings.py`
- Test: `tests/gecko/test_gecko_ledger.py`

**Interfaces:**
- Produces:
  - `entry_price(store, token_id, entered_at, *, as_of=None, allow_cg: bool = True)` — CoinGecko rows considered only when `allow_cg`.
  - `_resolve_entry` passes `allow_cg = now > deadline` (a CoinGecko price can no longer freeze while a DEX row may still arrive inside the 48 h window).
  - setting `ledger_max_overdue_days: int = 14`
  - `Store.close_unmeasured(token_id, horizon_days, measured_at) -> bool` — finalizes an open row with `ret`/`ret_vs_btc` NULL.
  - `LedgerReport.expired: int`; a row more than `ledger_max_overdue_days` past `due_at` is closed unmeasured (log `ledger_row_expired` with `due_at` and `attempts`) BEFORE any BTC lookup or fetch. This closes the "permanent 403" and "BTC gap at entry" rows that today stay due forever and page `forward_returns_backlog` every 6 h.

- [ ] **Step 1: Write the failing tests**

Append to `tests/gecko/test_gecko_ledger.py` (uses the file's `seed`, `E`, `DS`, `one`):

```python
async def test_coingecko_price_does_not_freeze_before_deadline(store, settings):
    await seed(store, token_obs=False)
    await store.add_observation("solana:abc", "coingecko", "2026-10-01T00:05:00Z", {"price_usd": 7.0})
    # 1-day horizon is due at +24 h; the 48 h entry window is still open
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(DS, payload=[{"priceUsd": "9.0", "liquidity": {"usd": 5}}], repeat=True)
            await measure_due(store, settings, s, "2026-10-02T02:00:00Z")
    assert await one(store, "SELECT entry_price FROM watchlist") == (None,)
    # a DEX row arrives inside the window and wins
    await store.add_observation("solana:abc", "dexscreener", "2026-10-02T03:00:00Z", {"price_usd": 3.0})
    assert await entry_price(store, "solana:abc", E, allow_cg=False) == 3.0


async def test_coingecko_fallback_used_after_deadline(store, settings):
    await seed(store, token_obs=False)
    await store.add_observation("solana:abc", "coingecko", "2026-10-01T00:05:00Z", {"price_usd": 7.0})
    await store.add_observation("benchmark:btc", "coingecko", "2026-10-03T01:00:00Z", {"price_usd": 61000.0})
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(DS, payload=[{"priceUsd": "14.0", "liquidity": {"usd": 5}}], repeat=True)
            await measure_due(store, settings, s, "2026-10-03T01:00:00Z")
    assert await one(store, "SELECT entry_price FROM watchlist") == (7.0,)


async def test_overdue_row_expires_unmeasured(store, settings):
    await seed(store, btc=False)  # BTC gap at entry: today this row would stay due forever
    later = "2026-10-20T00:00:00Z"  # 1-day row is 18 d overdue, 7-day row 12 d
    await store.add_observation("benchmark:btc", "coingecko", "2026-10-19T23:00:00Z", {"price_usd": 70000.0})
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as s:
            with aioresponses():
                r = await measure_due(store, settings, s, later)
    assert r.expired == 1
    rows = await (await store.conn.execute(
        "SELECT horizon_days, measured_at, ret FROM forward_returns WHERE horizon_days IN (1,7) "
        "ORDER BY horizon_days")).fetchall()
    assert rows == [(1, later, None), (7, None, None)]
    assert any(e["event"] == "ledger_row_expired" and e["horizon_days"] == 1 for e in logs)


async def test_close_unmeasured_is_write_once(store):
    await seed(store)
    assert await store.close_unmeasured("solana:abc", 1, "2026-10-20T00:00:00Z") is True
    assert await store.close_unmeasured("solana:abc", 1, "2026-10-21T00:00:00Z") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_ledger.py -q`
Expected: FAIL (`entry_price() got an unexpected keyword argument 'allow_cg'`; `LedgerReport` has no `expired`).

- [ ] **Step 3: Implement**

`gecko/settings.py`: `ledger_max_overdue_days: int = 14  # a due row still open this long closes unmeasured`.

`gecko/store.py` — add after `set_return`:

```python
    async def close_unmeasured(self, token_id, horizon_days, measured_at) -> bool:
        """Finalize an open row with no return (ret NULL). Write-once."""
        cur = await self.conn.execute(
            "UPDATE forward_returns SET measured_at=? WHERE token_id=? AND horizon_days=? "
            "AND measured_at IS NULL",
            (canon_ts(measured_at), token_id, horizon_days),
        )
        await self.conn.commit()
        return cur.rowcount == 1
```

`gecko/ledger.py`:

```python
async def entry_price(
    store: Store, token_id: str, entered_at: str, *, as_of: str | None = None, allow_cg: bool = True
) -> float | None:
    """...(keep docstring; add:) ``allow_cg`` False skips the CoinGecko fallback,
    which the ledger uses until the 48 h window closes so a DEX row arriving
    later inside the window is not pre-empted by an earlier CoinGecko price."""
    p = await _first_priced(store, token_id, DEX_SOURCES, entered_at, as_of)
    if p is None and allow_cg:
        p = await _first_priced(store, token_id, ("coingecko",), entered_at, as_of)
    return p
```

In `_resolve_entry`:

```python
    closed = now > deadline
    ep = await entry_price(store, tid, entered_at, as_of=min(now, deadline), allow_cg=closed)
    if ep is not None:
        await store.set_entry_price(tid, ep)
    return ep, closed
```

`LedgerReport` gains `expired: int = 0`. In `measure_due`, directly after the `if tid in closed: continue` line:

```python
        if parse_utc(now) - parse_utc(due.due_at) > timedelta(days=settings.ledger_max_overdue_days):
            if await store.close_unmeasured(tid, h, now):
                rep.expired += 1
                log.warning("ledger_row_expired", token_id=tid, horizon_days=h,
                            due_at=due.due_at, attempts=due.attempts)
            continue
```

`ledger_job` rows_written becomes `r.measured + r.total_loss + r.unmeasurable + r.expired`.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko/test_gecko_ledger.py -q`
Expected: PASS. If an existing test measured a row more than 14 days overdue and expected a real return, it now sees an expiry: re-date its `now` to within 14 days of `due_at` (the expiry is the fix; the old expectation encoded the stuck-forever behaviour). Report any such edit in the commit message.

- [ ] **Step 5: Commit**

```bash
git add gecko/ledger.py gecko/store.py gecko/settings.py tests/gecko/test_gecko_ledger.py
git commit -m "fix(gecko): ledger — CG entry fallback only after the 48h window; expire rows 14d overdue"
```

---

### Task 9: Backtest `--from/--to` and exit 1 on FAIL

**Files:**
- Modify: `gecko/backtest/runner.py`, `gecko/__main__.py`
- Test: `tests/gecko/test_gecko_backtest.py`

**Interfaces:**
- Produces: CLI `--from` / `--to` (ISO dates, inclusive from, exclusive to, compared via `canon_ts` against each coin's `t0`); the window applies to both the selected cohort and the same-month baseline universe; the job exits `1` when the battery verdict is not PASS (a prereg gate must fail loudly), `0` on PASS.

- [ ] **Step 1: Write the failing tests**

Edit `test_backtest_job_prints_row_and_divergence`: its two-coin cohort fails criterion 5 (`20 <= n`), so change `assert rc == 0` to `assert rc == 1` and add a comment `# n=2 fails the battery: a FAIL verdict exits 1`. Then append:

```python
def _run_backtest(tmp_path, monkeypatch, *extra):
    from gecko.__main__ import main

    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))
    return main(["backtest", "--rule", "R0", "--study-dir", str(tmp_path),
                 "--history-db", str(tmp_path / "scout.db"), *extra])


def test_backtest_window_filters_coins(tmp_path, capsys, monkeypatch):
    write_study(tmp_path, {"a": coin(10, 1.0, 8.0), "b": coin(12, 1.0, 0.4)})
    _run_backtest(tmp_path, monkeypatch, "--from", "2026-04-12", "--to", "2026-04-14")
    out = capsys.readouterr().out
    assert "\nR0 | 1 | " in out and "window 2026-04-12T00:00:00Z..2026-04-14T00:00:00Z" in out


def test_backtest_pass_exits_0(tmp_path, capsys, monkeypatch):
    # 24 young, volume-accelerating winners at 3.0x pass F4; 6 old losers at
    # 0.5x are F4-rejected but sit in the same months, so the cohort beats its
    # same-month baseline. t0 spread over ~4 months gives a median halves split.
    # Verified PASS against the Plan 1 battery on 2026-10-14.
    hot = [1] * 7 + [10] * 7  # prior week 7, last week 70: ratio 10
    coins = {f"w{i}": coin(10 + 5 * i, 1.0, 3.0, vols=hot) for i in range(24)}
    coins |= {f"l{i}": coin(10 + 5 * i, 1.0, 0.5, age_days=200, vols=hot) for i in range(6)}
    write_study(tmp_path, coins)
    from gecko.__main__ import main

    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))
    rc = main(["backtest", "--rule", "F4", "--study-dir", str(tmp_path),
               "--history-db", str(tmp_path / "scout.db")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "| PASS |" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/gecko/test_gecko_backtest.py -q`
Expected: FAIL (`unrecognized arguments: --from`; rc 0 where 1 expected).

- [ ] **Step 3: Implement**

`gecko/__main__.py` — after `--history-db`:

```python
    p.add_argument("--from", dest="date_from", default=None, help="backtest: first t0 (inclusive)")
    p.add_argument("--to", dest="date_to", default=None, help="backtest: last t0 (exclusive)")
```

`gecko/backtest/runner.py` — after `load_study`:

```python
    lo = canon_ts(ns.date_from) if getattr(ns, "date_from", None) else None
    hi = canon_ts(ns.date_to) if getattr(ns, "date_to", None) else None
    coins = [c for c in coins
             if (lo is None or canon_ts(c.t0) >= lo) and (hi is None or canon_ts(c.t0) < hi)]
    if lo or hi:
        print(f"window {lo or '-'}..{hi or '-'}")
```

(import `canon_ts` from `gecko.store`), and replace the final `return 0` with:

```python
    return 0 if b.passed else 1
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/gecko/test_gecko_backtest.py -q`
Expected: PASS. The F4 regression anchor (`docs/prereg/F4.md`, PASS) is unaffected: on the real study data the job still prints `PASS` and now exits 0.

- [ ] **Step 5: Commit**

```bash
git add gecko/backtest/runner.py gecko/__main__.py tests/gecko/test_gecko_backtest.py
git commit -m "feat(gecko): backtest --from/--to window; FAIL verdict exits 1"
```

---

### Task 10: Seam test, full verification, PR, reviewer barrier

**Files:**
- Create: `tests/gecko/test_gecko_seam.py`
- Create: `.reviewers/<PR>.toml` (after review)
- Modify: `docs/superpowers/plans/2026-10-09-gecko-core-package-followups.md` (strike the closed tickets, add Plan 2 tickets), CLI help test

**Interfaces:** Consumes everything above. Plan 1's lesson: cross-module defects were invisible to per-task reviews and surfaced only in a seam test and vector reviews.

- [ ] **Step 1: Write the seam test**

```python
# tests/gecko/test_gecko_seam.py
"""Telegram call -> DexScreener discovery -> GoPlus enrichment -> resolution -> features."""

from datetime import datetime, timezone
from types import SimpleNamespace

import aiohttp
from aioresponses import aioresponses

from gecko.collectors.dexscreener import BOOSTS_URL, PROFILES_URL, TOKENS_URL, collect
from gecko.collectors.goplus import enrich
from gecko.features import compute_features
from gecko.telegram_reader import poll_once

ADDR = "0x" + "e" * 40
NOW = "2026-10-14T12:00:00Z"
GP = "https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses=" + ADDR


class Client:
    async def get_messages(self, entity, limit, min_id):
        return [SimpleNamespace(id=1, date=datetime(2026, 10, 14, 11, tzinfo=timezone.utc),
                                message=f"new gem {ADDR}")]


async def _nosleep(_):
    return None


async def test_call_to_features(store, settings):
    settings.chains = ["base"]
    await poll_once(store, Client(), settings, {"@c": "@c"}, {}, now=NOW)
    pair = {"chainId": "base", "dexId": "u", "pairAddress": "0xp",
            "baseToken": {"address": ADDR, "symbol": "E", "name": "E"},
            "priceUsd": "1", "liquidity": {"usd": 400000}, "marketCap": 2e6,
            "volume": {"h24": 1}, "txns": {"h24": {}}, "pairCreatedAt": 1_759_000_000_000}
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BOOSTS_URL, payload=[])
            m.get(PROFILES_URL, payload=[])
            m.get(TOKENS_URL.format(chain="base", addresses=ADDR), payload=[pair])
            await collect(store, settings, s, NOW, sleep=_nosleep)
            m.get(GP, payload={"code": 1, "result": {ADDR: {
                "is_honeypot": "0", "holder_count": "500",
                "holders": [{"address": "0x" + "1" * 40, "percent": "0.05", "is_contract": 0}]}}})
            await enrich(store, settings, s, NOW, sleep=_nosleep, budget_now=NOW)
    tid = f"base:{ADDR}"
    # the duplicate message is ignored; the poll's resolution pass maps the stored call
    await poll_once(store, Client(), settings, {"@c": "@c"}, {"@c": 1}, now=NOW)
    row = await (await store.conn.execute(
        "SELECT t.first_source, sc.token_id FROM tokens t JOIN source_calls sc ON sc.token_id=t.token_id")).fetchone()
    assert row == ("telegram", tid)
    f = await compute_features(store, settings, tid, NOW)
    assert f.callers_7d == 1 and f.security_ok is True and f.holders == 500
    assert f.top10_pct == 5.0
```

- [ ] **Step 2: Run the seam test**

Run: `python -m pytest tests/gecko/test_gecko_seam.py -q`
Expected: PASS. If it fails, the failure is a cross-module defect: fix it in the owning module (with a unit test there), not by loosening the seam test.

- [ ] **Step 3: Extend the CLI help test**

In `tests/gecko/test_gecko_cli.py::test_help_epilog_lists_jobs_and_exit_codes` extend the job tuple with `"enrich", "telegram-reader", "tg-login", "migrate-calls", "prune"`.

- [ ] **Step 4: Full verification (capture the REAL exit code — never through `| tail`)**

```bash
python -m pytest tests/gecko -q; echo "EXIT=$?"
python -m black --check gecko/ tests/gecko/; echo "EXIT=$?"
python scripts/gecko_loc_guard.py; echo "EXIT=$?"
python -m pytest tests/ -q -x --ignore=tests/gecko; echo "EXIT=$?"   # legacy suite must not regress
```

Expected: every `EXIT=0`; guard prints `gecko=<= 3600 tests=<= 7500`. If the guard trips, stop and report to the controller with the numbers — do not trim tests to fit.

- [ ] **Step 5: Update the follow-ups doc**

In `docs/superpowers/plans/2026-10-09-gecko-core-package-followups.md` mark CLOSED (with this branch's commit SHAs): observations retention / prune; runs retention; ledger permanent-403 + BTC-gap finalizer; `_resolve_entry` CG fallback; backtest flags + exit 1; tests-limit boundary test. Add a "From Plan 2" section listing: GoPlus CU/call calibration (soak); `gp_daily_cap` tuning; cashtag-only calls never resolve (by design, v1); a call resolved after `as_of` is visible to a replayed `as_of` (live-only ranking, accepted); X `ingest-call` CLI deferred to Plan 3 with the X lane.

- [ ] **Step 6: Commit, push, open PR**

```bash
git add tests/gecko/ docs/superpowers/plans/2026-10-09-gecko-core-package-followups.md
git commit -m "test(gecko): Plan 2 seam test; CLI lists new jobs; follow-ups updated"
git push -u origin plan/gecko-plan2-enrichment
gh pr create --title "feat(gecko): GoPlus enrichment + Telegram callers (Plan 2 of 3)" --body-file <(cat <<'EOF'
Plan: docs/superpowers/plans/2026-10-14-gecko-enrichment-telegram.md
Spec amendments dated 2026-10-14 (S1 3600/7500, GoPlus-only, own TG session).
Nothing deployed; Plan 3 owns deploy.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)
```

- [ ] **Step 7: Reviewer barrier (CLAUDE.md REVIEW BARRIER; `reviewer-clearances` is a required check)**

Dispatch the four mandatory vectors as independent reviewers on the PR head, each with its own attack-vector prompt (not a generic review):
- **concurrency** — reader + enrich + dexscreener + rank + prune writing one SQLite file concurrently; batch-delete commit cadence; `INSERT OR IGNORE` idempotency under overlap; two `enrich` runs overlapping the daily cap.
- **logic** — GoPlus flag semantics vs the spec amendment; top-10 exclusions; queue rules; resolution ambiguity; ledger expiry ordering vs BTC lookup; backtest window applied to the baseline.
- **ops-safety** — session-file handling, keyless rate limits, prune on a 7 GB-class DB, `.env.gecko` only, nothing touches legacy `scout/` or its `.env`, migrate-calls read-only.
- **silent-failure** — unknown-as-safe, run rows `ok` when nothing was actually read, freshness keys that can never breach, logs that leak `str(exc)`.

Loop fix → re-review until every vector is terminal and every finding is dispositioned; any code change after a clearance re-runs the vectors that change could invalidate. Then write `.reviewers/<PR>.toml` (template: `.reviewers/597.toml`, same `required` and `watch` lists, clearances = the last code SHA each vector cleared), commit, and wait for exact-head CI green (`gh run view --json conclusion`, not the watch stream).

- [ ] **Step 8: Merge ask**

Merge needs the operator's recorded per-PR approval (Approvals Discipline). Report: tests/exit codes, LOC numbers, vector verdicts with SHAs, every spec amendment, and the Plan 3 checklist additions below.

## Plan 3 checklist additions (record in the follow-ups doc in Step 5)

- Run `python -m gecko tg-login` once on the VPS as the gecko user with `GECKO_TG_SESSION_PATH=/root/.gecko/gecko_tg.session` (outside the repo); confirm mode 0600.
- `python -m gecko migrate-calls --history-db /root/gecko-history/scout.db` AFTER the read-only copy exists; paste its `GECKO_TG_CHANNELS=` line into `.env.gecko`.
- `gecko-telegram-reader.service`: `Restart=always`, `RestartSec=30`, `WorkingDirectory=/root/gecko-alpha`.
- Cron: `enrich` hourly (stagger off :00), `prune` daily (off-peak, after backup).
- Calibrate `gp_daily_cap` from the GoPlus dashboard after day 1; measure observation bytes/row and set `GECKO_OBS_RETENTION_DAYS` from the disk headroom (floor 30).
- Plan 3 flips `require_security_known=True` only after `enrich` has covered every active token at least once.

## Self-review (done while writing)

- **Spec coverage:** §5 GoPlus (Tasks 1–2), Telegram callers (Tasks 3–6), discovery breadth S2 (Task 4), §4 retention (Task 7), §9 ledger tickets (Task 8), §10/S5 backtest gate (Task 9), §12 freshness (Tasks 2, 5), §14.4 handover replaced (Task 0). GMGN smart money (`smart_money_wallets`, `dd_score`) is NOT in this plan — spec §17 already makes it NULL-tolerant and demo-key-gated; it stays a Plan 3/later item. X `ingest-call` deferred with the X lane.
- **Placeholders:** none. The Task 9 PASS fixture was run against the Plan 1 battery on 2026-10-14 and passes.
- **Type consistency:** `insert_call(..., event_id=...)` keyword shares its name with the module-level `event_id()` function; inside `insert_call` the parameter shadows the function, which is not called there, so it is harmless. Keep the name across Tasks 3, 5, 6 and 10 (all call sites use the keyword).
- **Review Focus:** each of the five lines has a named test in its owning task.
