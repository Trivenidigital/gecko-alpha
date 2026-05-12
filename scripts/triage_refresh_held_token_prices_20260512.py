"""One-off triage: refresh price_cache for tokens currently held in open paper trades.

DO NOT SCHEDULE THIS AS A RECURRING JOB.

This script is the symptom-level triage for the gap described in
`tasks/findings_open_position_price_freshness_2026_05_12.md`. The architectural
fix (held-position price-refresh lane) is deferred to a next-session design
pass. Running this script entrenches the symptom-level patch and reduces
urgency on the underlying fix — that is the exact failure mode the finding
warns against.

The script:
  1. Reads currently-held token_ids from `paper_trades WHERE status='open'`
  2. Filters to tokens that (a) look like CoinGecko coin_ids and
     (b) have stale (> 1h) or missing `price_cache` rows
  3. Issues a single batched `/simple/price` request to CoinGecko (free tier)
  4. Upserts `price_cache` via `INSERT ... ON CONFLICT DO UPDATE`,
     preserving `price_change_7d` on existing rows
  5. Writes a JSON log file with per-token old_price / new_price / delta_pct
  6. Does NOT touch evaluator state, does NOT trigger exits

Blast radius: bounded to `price_cache` writes + one CoinGecko API call.
The evaluator's next cycle reads the refreshed prices and decides independently.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

import aiohttp
import aiosqlite

DB_PATH = "/root/gecko-alpha/scout.db"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
STALE_THRESHOLD_HOURS = 1.0
BATCH_SIZE = 250
SLEEP_BETWEEN_BATCHES_SEC = 2.5


def is_cg_coin_id(token_id: str | None) -> bool:
    """Heuristic: skip obvious contract addresses; pass everything else.

    CoinGecko coin_ids are lowercase alphanumeric + hyphens + underscores.
    Contract addresses start with 0x (EVM) or are long base58 strings (Solana).
    Permissive on the CG side, strict on the obvious-contract side — false
    negatives in CG just produce `not_found_in_cg` log entries which is fine.
    """
    if not token_id:
        return False
    if token_id.startswith("0x"):
        return False
    if len(token_id) > 60:
        return False
    return all(c.isalnum() or c in "-_" for c in token_id.lower())


async def get_held_tokens(db: aiosqlite.Connection) -> list[dict]:
    query = """
        SELECT pt.token_id,
               MAX(pt.symbol) AS symbol,
               pc.current_price,
               (julianday('now') - julianday(pc.updated_at)) * 24.0 AS age_hours
          FROM paper_trades pt
          LEFT JOIN price_cache pc ON pt.token_id = pc.coin_id
         WHERE pt.status = 'open'
         GROUP BY pt.token_id
    """
    rows = []
    async with db.execute(query) as cursor:
        async for row in cursor:
            rows.append(
                {
                    "token_id": row[0],
                    "symbol": row[1] or "",
                    "old_price": row[2],
                    "age_hours": row[3],
                }
            )
    return rows


async def fetch_prices_batch(
    session: aiohttp.ClientSession, coin_ids: list[str]
) -> dict:
    if not coin_ids:
        return {}
    url = f"{COINGECKO_BASE}/simple/price"
    params = {
        "ids": ",".join(coin_ids),
        "vs_currencies": "usd",
        "include_market_cap": "true",
        "include_24hr_change": "true",
    }
    async with session.get(
        url, params=params, timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        if resp.status != 200:
            text = await resp.text()
            print(f"[err] CoinGecko returned {resp.status}: {text[:200]}")
            return {}
        return await resp.json()


async def main() -> int:
    started_at = datetime.now(timezone.utc)
    triage_log: list[dict] = []

    async with aiosqlite.connect(DB_PATH) as db:
        held = await get_held_tokens(db)

    print(f"[info] {len(held)} unique held tokens in open paper_trades")

    eligible: list[dict] = []
    skipped_contract_addr = 0
    skipped_fresh = 0
    for h in held:
        if not is_cg_coin_id(h["token_id"]):
            skipped_contract_addr += 1
            continue
        age = h["age_hours"]
        if age is not None and age < STALE_THRESHOLD_HOURS:
            skipped_fresh += 1
            continue
        eligible.append(h)

    print(
        f"[info] {len(eligible)} eligible for refresh "
        f"(skipped: {skipped_contract_addr} contract-addr-shaped, "
        f"{skipped_fresh} cache-fresh < {STALE_THRESHOLD_HOURS}h)"
    )

    if not eligible:
        print("[info] nothing to refresh; exiting clean")
        return 0

    async with aiohttp.ClientSession() as session:
        async with aiosqlite.connect(DB_PATH) as db:
            for i in range(0, len(eligible), BATCH_SIZE):
                batch = eligible[i : i + BATCH_SIZE]
                ids = [h["token_id"] for h in batch]
                print(
                    f"[info] fetching {len(ids)} prices "
                    f"(batch {i // BATCH_SIZE + 1})"
                )
                prices = await fetch_prices_batch(session, ids)

                for h in batch:
                    entry = {
                        "token_id": h["token_id"],
                        "symbol": h["symbol"],
                        "old_price": h["old_price"],
                        "old_age_hours": (
                            round(h["age_hours"], 1)
                            if h["age_hours"] is not None
                            else None
                        ),
                    }
                    p = prices.get(h["token_id"])
                    if not p:
                        entry.update(
                            {"new_price": None, "delta_pct": None,
                             "result": "not_found_in_cg"}
                        )
                        triage_log.append(entry)
                        continue
                    new_price = p.get("usd")
                    if new_price is None:
                        entry.update(
                            {"new_price": None, "delta_pct": None,
                             "result": "no_usd_price"}
                        )
                        triage_log.append(entry)
                        continue

                    delta_pct = None
                    if h["old_price"] is not None and h["old_price"] > 0:
                        delta_pct = (
                            (new_price - h["old_price"]) / h["old_price"]
                        ) * 100.0

                    await db.execute(
                        """INSERT INTO price_cache
                             (coin_id, current_price, price_change_24h,
                              market_cap, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(coin_id) DO UPDATE SET
                             current_price = excluded.current_price,
                             price_change_24h = excluded.price_change_24h,
                             market_cap = excluded.market_cap,
                             updated_at = excluded.updated_at""",
                        (
                            h["token_id"],
                            new_price,
                            p.get("usd_24h_change"),
                            p.get("usd_market_cap"),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )

                    entry.update(
                        {
                            "new_price": new_price,
                            "delta_pct": (
                                round(delta_pct, 4)
                                if delta_pct is not None
                                else None
                            ),
                            "result": "refreshed",
                        }
                    )
                    triage_log.append(entry)

                if i + BATCH_SIZE < len(eligible):
                    await asyncio.sleep(SLEEP_BETWEEN_BATCHES_SEC)

            await db.commit()

    finished_at = datetime.now(timezone.utc)

    refreshed = [e for e in triage_log if e["result"] == "refreshed"]
    material_drift = [
        e
        for e in refreshed
        if e["delta_pct"] is not None and abs(e["delta_pct"]) > 10.0
    ]

    summary = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "wall_clock_sec": round(
            (finished_at - started_at).total_seconds(), 1
        ),
        "total_held": len(held),
        "skipped_contract_addr": skipped_contract_addr,
        "skipped_fresh": skipped_fresh,
        "eligible": len(eligible),
        "refreshed": len(refreshed),
        "not_found_in_cg": sum(
            1 for e in triage_log if e["result"] == "not_found_in_cg"
        ),
        "no_usd_price": sum(
            1 for e in triage_log if e["result"] == "no_usd_price"
        ),
        "material_drift_count": len(material_drift),
        "material_drift_pct_of_refreshed": (
            round(len(material_drift) / len(refreshed) * 100, 1)
            if refreshed
            else 0
        ),
        "top20_drift": [
            {
                "symbol": e["symbol"],
                "token_id": e["token_id"],
                "old_price": e["old_price"],
                "new_price": e["new_price"],
                "delta_pct": e["delta_pct"],
                "stale_hours": e["old_age_hours"],
            }
            for e in sorted(
                refreshed,
                key=lambda x: (
                    abs(x["delta_pct"]) if x["delta_pct"] is not None else 0
                ),
                reverse=True,
            )[:20]
        ],
    }

    print(json.dumps(summary, indent=2))

    log_path = (
        f"/root/gecko-alpha/triage_price_refresh_"
        f"{started_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    with open(log_path, "w") as f:
        json.dump(
            {"summary": summary, "entries": triage_log}, f, indent=2
        )
    print(f"[info] log written to {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
