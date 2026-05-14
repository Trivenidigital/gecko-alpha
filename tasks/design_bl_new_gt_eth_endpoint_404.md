**New primitives introduced:** Static GeckoTerminal network-id alias map inside the existing GeckoTerminal ingestion module.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| GeckoTerminal network-id resolution | None installed on VPS. Installed skills include `coin_resolver`, `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`, `xurl`, and dev/productivity skills, but no GeckoTerminal network-id resolver. | Build the alias inside existing `scout/ingestion/geckoterminal.py`. |
| Public Hermes skill hub / crypto skills | No public Hermes skill found for GeckoTerminal network-id normalization or DEX trending-pools ingestion. Optional blockchain skills are chain/RPC-oriented and do not replace this ingestion lane. | Keep local ingestion code; use official GeckoTerminal network metadata as source of truth. |
| awesome-hermes-agent ecosystem | No suitable GeckoTerminal resolver/ingestion skill found in `0xNyk/awesome-hermes-agent`; listed resources are mostly agent framework, workspace, gateway, and general skills. | No replacement available. |

Self-Evolution Kit check: `NousResearch/hermes-agent-self-evolution` is not a runtime market-data resolver and does not apply.

Verdict: custom code is justified, but it should be the smallest possible alias fold in the existing ingestion module.

## Evidence

Backlog item: `BL-NEW-GT-ETH-ENDPOINT-404` was filed after cycle-change audit observed about 40 GeckoTerminal 404 errors/hr for the `ethereum` chain.

Runtime evidence on srilu after PR #127 deploy:

- `.env`: `CHAINS=["solana","base","ethereum"]`, `CHAINS_ENABLED=true`
- journalctl repeats `geckoterminal_non_retryable_status` with URL `https://api.geckoterminal.com/api/v2/networks/ethereum/trending_pools` and status `404`

Live endpoint check from the worktree:

- `https://api.geckoterminal.com/api/v2/networks/ethereum/trending_pools` -> `404`
- `https://api.geckoterminal.com/api/v2/networks/eth/trending_pools` -> `200`
- `base` and `solana` -> `200`

Official GeckoTerminal network metadata at `https://api.geckoterminal.com/api/v2/networks?page=1` lists:

- `id="eth"`, `name="Ethereum"`, `coingecko_asset_platform_id="ethereum"`
- `id="base"`, `name="Base"`
- `id="solana"`, `name="Solana"`

Root cause: gecko-alpha's canonical project chain label is `ethereum`, but GeckoTerminal's URL network id is `eth`.

## Design

Add one private helper in `scout/ingestion/geckoterminal.py`:

```python
GECKOTERMINAL_NETWORK_BY_CHAIN = {
    "ethereum": "eth",
}

def _geckoterminal_network_for_chain(chain: str) -> str:
    return GECKOTERMINAL_NETWORK_BY_CHAIN.get(chain, chain)
```

Use the helper only when building the GeckoTerminal URL:

```python
network = _geckoterminal_network_for_chain(chain)
url = f"{GECKO_BASE}/networks/{network}/trending_pools"
```

Do not rewrite the project chain label. `CandidateToken.from_geckoterminal(pool, chain=chain)` still receives `ethereum`, watchdog samples still use `geckoterminal:ethereum`, and downstream DB/scoring semantics remain unchanged.

This avoids a broader config migration from `ethereum` to `eth`, which would leak provider-specific IDs into project-owned chain labels and likely break narrative, Telegram, trading, and dashboard code that already treats `ethereum` as canonical.

## Tests

Add focused coverage in `tests/test_geckoterminal.py`:

- `CHAINS=["ethereum"]` requests `/networks/eth/trending_pools`
- returned token keeps `chain == "ethereum"`
- watchdog sample source remains `geckoterminal:ethereum`

Adjust the permanent-404 test to use an actually unknown network id, because `ethereum` should no longer produce the broken URL.

## Operational Verification

After deploy:

```bash
journalctl -u gecko-pipeline --since=-10min --no-pager -l | grep geckoterminal_non_retryable_status | grep ethereum
```

Expected: no new `networks/ethereum/trending_pools` 404 lines after restart.

Positive check:

```bash
journalctl -u gecko-pipeline --since=-10min --no-pager -l | grep 'geckoterminal:ethereum\\|networks/eth/trending_pools' | tail
```

Expected: Ethereum remains represented as the project chain/source label while the provider URL uses `/networks/eth/`.

