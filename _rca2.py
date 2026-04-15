"""RCA: Check all CoinGecko Highlights tokens against our system."""
import asyncio
from scout.db import Database

async def rca():
    db = Database("scout.db")
    await db.initialize()
    conn = db._conn

    targets = [
        ("RaveDAO", "rave", "ravedao", "+149.5%", "trending"),
        ("Enjin Coin", "enj", "enjincoin", "+31.3%", "trending"),
        ("Aria.AI", "aria", "aria-ai", "-84.7%", "trending"),
        ("Bittensor", "tao", "bittensor", "-4.7%", "trending"),
        ("MYX Finance", "myx", "myx-finance", "+21.8%", "trending"),
        ("Polkadot", "dot", "polkadot", "-4.0%", "trending"),
        ("Pudgy Penguins", "pengu", "pudgy-penguins", "-1.3%", "trending"),
        ("Monad", "mon", "monad", "-0.1%", "trending"),
        ("aPriori", "apriori", "apriori", "+68.8%", "gainers"),
        ("BinanceLife", "binancelife", "binancelife", "+68.0%", "gainers"),
        ("Bedrock", "br", "bedrock-defi", "+43.4%", "gainers"),
        ("Anoma", "anoma", "anoma", "+36.9%", "gainers"),
        ("PUMPCADE", "pumpcade", "pumpcade", "+35.5%", "gainers"),
        ("CommonWealth", "wlth", "commonwealth", "+35.8%", "gainers"),
        ("BUILDon", "buildon", "buildon", "-39.0%", "losers"),
        ("BCGame Coin", "bcgame", "bc-game", "-25.0%", "losers"),
        ("LAB", "lab", "lab-2", "-20.8%", "losers"),
        ("MEZO", "mezo", "mezo", "-10.5%", "losers"),
        ("Ultima", "ultima", "ultima", "-18.2%", "losers"),
        ("Bless", "bless", "bless-2", "-7.5%", "losers"),
        ("Infinex", "infinex", "infinex", "-15.7%", "losers"),
        ("Irys", "irys", "irys", "-14.6%", "losers"),
    ]

    caught = 0
    missed = 0
    missed_list = []

    for name, ticker, slug, change, section in targets:
        found = False
        methods = []

        # Trending comparisons
        cursor = await conn.execute(
            "SELECT * FROM trending_comparisons WHERE LOWER(coin_id)=? OR LOWER(symbol)=? LIMIT 1",
            (slug.lower(), ticker.lower())
        )
        row = await cursor.fetchone()
        if row and not row["is_gap"]:
            found = True
            lead = row["chains_lead_minutes"] or row["narrative_lead_minutes"] or row["pipeline_lead_minutes"]
            methods.append("trending(" + (str(round(lead)) + "min" if lead else "?") + ")")

        # Candidates
        cursor = await conn.execute(
            "SELECT quant_score FROM candidates WHERE LOWER(ticker)=? OR LOWER(token_name) LIKE ? LIMIT 1",
            (ticker.lower(), "%" + name.lower()[:10] + "%")
        )
        row = await cursor.fetchone()
        if row:
            found = True
            methods.append("candidate(q=" + str(row["quant_score"]) + ")")

        # Predictions
        cursor = await conn.execute(
            "SELECT category_name, narrative_fit_score, is_control FROM predictions WHERE LOWER(symbol)=? OR LOWER(coin_id) LIKE ? LIMIT 1",
            (ticker.lower(), "%" + slug.lower()[:10] + "%")
        )
        row = await cursor.fetchone()
        if row:
            found = True
            c = "[C]" if row["is_control"] else ""
            methods.append("predict(" + str(row["category_name"]) + c + ")")

        # Chain events
        cursor = await conn.execute(
            "SELECT event_type FROM signal_events WHERE LOWER(token_id) LIKE ? LIMIT 1",
            ("%" + slug.lower()[:10] + "%",)
        )
        row = await cursor.fetchone()
        if row:
            found = True
            methods.append("chain")

        # Volume spikes
        cursor = await conn.execute(
            "SELECT spike_ratio FROM volume_spikes WHERE LOWER(coin_id) LIKE ? LIMIT 1",
            ("%" + slug.lower()[:10] + "%",)
        )
        row = await cursor.fetchone()
        if row:
            found = True
            methods.append("spike(" + str(round(row["spike_ratio"], 1)) + "x)")

        # Paper trades
        cursor = await conn.execute(
            "SELECT signal_type, status FROM paper_trades WHERE LOWER(token_id) LIKE ? LIMIT 1",
            ("%" + slug.lower()[:10] + "%",)
        )
        row = await cursor.fetchone()
        if row:
            methods.append("trade(" + row["status"] + ")")

        status = "CAUGHT" if found else "MISSED"
        if found:
            caught += 1
        else:
            missed += 1
            missed_list.append((name, change, section))

        via = " | ".join(methods) if methods else "---"
        print(f"  {status:6s}  [{section:8s}]  {name:20s} {change:>8s}  {via}")

    total = caught + missed
    pct = caught / total * 100 if total > 0 else 0
    print(f"\n=== RESULT: {caught}/{total} caught ({pct:.0f}%) | {missed} missed ===")
    if missed_list:
        print("\nMISSED tokens (need improvement):")
        for n, c, s in missed_list:
            print(f"  [{s}] {n} ({c})")

    await db.close()

asyncio.run(rca())
