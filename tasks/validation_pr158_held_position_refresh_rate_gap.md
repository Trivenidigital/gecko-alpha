# PR #158 Post-Deploy Validation Prep

Purpose: collect evidence for `BL-NEW-HELD-POSITION-REFRESH-RATE-GAP` after PR #158 is deployed and at least one pipeline cycle has completed.

Do not mark 24h validation complete until journal evidence exists.

Current deployment check on 2026-05-18:
- VPS `/root/gecko-alpha` was on `master` at `cdeb31f`, not PR #158.
- No post-deploy journal evidence was collected.
- This file is prep-only until #158 is deployed.

## Known Stale Cohort

Compare WARN/missing-id evidence against this documented stale cohort from `tasks/findings_held_position_refresh_rate_gap_2026_05_18.md`:

```text
pythia
argentine-football-association-fan-token
fartboy
iagon
kekius-maximus
secret
navi
prometeus
ready
olaxbt
marcopolo
safecoin
kinetiq
anthropic-prestocks-2
bityuan
manyu-2
meme-horse
hippo-protocol
superwalk
circle-internet-group-ondo-tokenized-stock
folks
```

## Step 1: Confirm Deployed Commit

Run SSH with redirect first, then read the file separately. Do not append `&& cat`.

```bash
ssh root@srilu-vps 'set -e; cd /root/gecko-alpha; echo BRANCH=$(git branch --show-current); echo HEAD=$(git rev-parse --short HEAD); git log -1 --oneline; grep -R "simple_price_missing_ids\|held_position_token_persistently_stale" -n scout/ingestion/held_position_prices.py' > .ssh_pr158_deploy_check.txt 2>&1
```

Then read:

```bash
Get-Content .ssh_pr158_deploy_check.txt
```

Proceed only if `HEAD` is the deployed PR #158 merge commit or contains PR #158's fields.

## Step 2: Collect One-Cycle Journal Evidence

After at least one pipeline cycle has completed post-deploy:

```bash
ssh root@srilu-vps 'journalctl -u gecko-pipeline --since "90 minutes ago" --no-pager | grep -E "held_position_refresh_summary|simple_price_missing_ids|held_position_token_persistently_stale"' > .ssh_pr158_journal_evidence.txt 2>&1
```

Then read:

```bash
Get-Content .ssh_pr158_journal_evidence.txt
```

Required evidence:
- At least one `held_position_refresh_summary`.
- `simple_price_missing_ids` present on the summary event.
- Any `held_position_token_persistently_stale` WARNs include `paper_trade_id`, `symbol`, `token_id`, and `consequence`.

## Step 3: Compare Overlap

Extract token ids from WARN rows and from `simple_price_missing_ids`, then compare against the known stale cohort above.

Interpretation:
- High overlap: stale-source hypothesis remains consistent; proceed to manual `/coins/{id}` recovery check.
- Low overlap: do not promote fallback yet. Investigate `simple_price_missing_ids`, CoinGecko 429/backoff state, and whether tokens returned by `/simple/price` were correctly excluded from WARN/gauge false positives.
- No WARNs but non-empty `simple_price_missing_ids`: inspect `stale_open_count`, cache ages, and the `HELD_POSITION_STALE_WARN_HOURS` runtime value before deciding.

## Step 4: Manual `/coins/{id}` Recovery Check

Only after CoinGecko rate limit clears, manually test at least one token that appears in post-deploy `simple_price_missing_ids`:

```bash
ssh root@srilu-vps 'python3 - <<'"'"'PY'"'"'
import json
import urllib.request

token_id = "pythia"
url = f"https://api.coingecko.com/api/v3/coins/{token_id}?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false"
req = urllib.request.Request(url, headers={"accept": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", "replace")
        print("status", resp.status)
        data = json.loads(body)
        print("id", data.get("id"))
        print("usd", (((data.get("market_data") or {}).get("current_price") or {}).get("usd")))
except Exception as exc:
    print(type(exc).__name__, exc)
PY' > .ssh_pr158_coins_endpoint_probe.txt 2>&1
```

Then read:

```bash
Get-Content .ssh_pr158_coins_endpoint_probe.txt
```

Promotion gate for `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT`:
- Promote only if `/coins/{id}` returns HTTP 200 with a usable USD price for at least one token that `/simple/price` missed in the post-deploy evidence.
- If `/coins/{id}` also fails or has no USD price, update backlog with that evidence and keep the fallback deferred.
