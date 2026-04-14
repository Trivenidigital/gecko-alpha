"""Check if our system detected any of the CoinGecko Highlights tokens."""
import asyncio
from scout.db import Database

async def check():
    db = Database("scout.db")
    await db.initialize()
    conn = db._conn

    targets = [
        ("RaveDAO", "rave", "ravedao"),
        ("Genius", "genius", "genius-ai"),
        ("Bless", "bless", "bless-network"),
        ("Pudgy Penguins", "pengu", "pudgy-penguins"),
        ("Hyperliquid", "hype", "hyperliquid"),
        ("Bitcoin", "btc", "bitcoin"),
        ("Bittensor", "tao", "bittensor"),
        ("Venice Token", "venice", "venice-token"),
        ("MEZO", "mezo", "mezo"),
        ("HumidiFi", "humid", "humidifi"),
        ("Irys", "irys", "irys"),
        ("Giggle Fund", "gig", "giggle-fund"),
    ]

    print("=== DID WE CATCH THESE BEFORE TRENDING? ===\n")

    found_any = False
    for tname, ticker, slug in targets:
        hits = []

        # Check predictions
        async with conn.execute(
            "SELECT symbol, category_name, narrative_fit_score, predicted_at, outcome_class, is_control FROM predictions WHERE LOWER(symbol)=? OR LOWER(coin_id)=?",
            (ticker.lower(), slug.lower())
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            ctrl = " [CTRL]" if r["is_control"] else ""
            hits.append(f"  PREDICTION: {r['symbol']} in {r['category_name']} fit={r['narrative_fit_score']} at {r['predicted_at']}{ctrl}")

        # Check candidates
        async with conn.execute(
            "SELECT token_name, ticker, chain, quant_score, conviction_score, first_seen_at FROM candidates WHERE LOWER(ticker)=? OR LOWER(token_name) LIKE ?",
            (ticker.lower(), f"%{tname.lower()[:8]}%")
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            hits.append(f"  CANDIDATE: {r['token_name']} ({r['ticker']}) quant={r['quant_score']} seen={r['first_seen_at']}")

        # Check chain events
        async with conn.execute(
            "SELECT event_type, pipeline, created_at FROM signal_events WHERE LOWER(token_id)=? OR LOWER(token_id)=? LIMIT 3",
            (slug.lower(), ticker.lower())
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            hits.append(f"  CHAIN: {r['event_type']} [{r['pipeline']}] at {r['created_at']}")

        if hits:
            found_any = True
            print(f"--- {tname} ({ticker}) --- FOUND!")
            for h in hits:
                print(h)
            print()
        else:
            print(f"--- {tname} ({ticker}) --- NOT FOUND")

    if not found_any:
        print("\n*** NONE of the trending tokens were detected by our system ***")

    print("\n=== SYSTEM STATUS ===")
    async with conn.execute("SELECT COUNT(*) as c FROM predictions WHERE is_control=0") as cur:
        row = await cur.fetchone()
    print(f"Agent predictions (non-control): {row['c']}")

    async with conn.execute("SELECT COUNT(*) as c FROM alerts") as cur:
        row = await cur.fetchone()
    print(f"Alerts sent: {row['c']}")

    async with conn.execute("SELECT COUNT(DISTINCT category_name) as c FROM narrative_signals") as cur:
        row = await cur.fetchone()
    print(f"Unique heating categories: {row['c']}")

    async with conn.execute("SELECT COUNT(*) as c FROM learn_logs") as cur:
        row = await cur.fetchone()
    print(f"Learn cycles: {row['c']}")

    print("\n=== WHY NO AGENT PREDICTIONS? ===")
    # Check if Claude scoring ever ran
    async with conn.execute("SELECT COUNT(*) as c FROM predictions WHERE is_control=0") as cur:
        row = await cur.fetchone()
    if row['c'] == 0:
        print("ANTHROPIC_API_KEY is 'placeholder' — Claude calls fail silently.")
        print("The 3-consecutive-failure bailout kicks in every cycle.")
        print("Result: only control picks (random, no Claude scoring) are stored.")
        print("")
        print("FIX: Set a real ANTHROPIC_API_KEY in .env, or switch to a free model.")

    await db.close()

asyncio.run(check())
