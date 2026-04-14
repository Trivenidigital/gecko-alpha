"""Status report script — copy to VPS and run."""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from scout.db import Database

async def report():
    db = Database("scout.db")
    await db.initialize()
    conn = db._conn
    now = datetime.now(timezone.utc)

    print("=== GECKO-ALPHA STATUS REPORT ===")
    print(f"Report time: {now.isoformat()}")

    # Uptime
    async with conn.execute("SELECT MIN(snapshot_at) as first FROM category_snapshots") as cur:
        row = await cur.fetchone()
    if row["first"]:
        first = datetime.fromisoformat(row["first"])
        hours = (now - first).total_seconds() / 3600
        print(f"Agent uptime: {hours:.0f} hours ({hours/24:.1f} days)")

    # Table counts
    print("\n--- DATA VOLUMES ---")
    tables = ["category_snapshots", "narrative_signals", "predictions", "signal_events",
              "active_chains", "chain_matches", "chain_patterns", "second_wave_candidates",
              "candidates", "alerts", "agent_strategy", "learn_logs"]
    for table in tables:
        try:
            async with conn.execute(f"SELECT COUNT(*) as c FROM {table}") as cur:
                row = await cur.fetchone()
            print(f"  {table}: {row['c']}")
        except Exception as e:
            print(f"  {table}: ERROR {e}")

    # Predictions
    print("\n--- NARRATIVE PREDICTIONS ---")
    async with conn.execute("""
        SELECT symbol, category_name, narrative_fit_score, confidence,
               counter_risk_score, market_regime, trigger_count,
               is_control, outcome_class,
               outcome_6h_change_pct, outcome_24h_change_pct, outcome_48h_change_pct,
               peak_change_pct, price_at_prediction, predicted_at, watchlist_users
        FROM predictions ORDER BY predicted_at DESC
    """) as cur:
        rows = await cur.fetchall()
    agent_rows = [r for r in rows if not r["is_control"]]
    ctrl_rows = [r for r in rows if r["is_control"]]
    print(f"  Total: {len(rows)} (agent={len(agent_rows)}, control={len(ctrl_rows)})")

    for r in rows:
        ctrl = " [CTRL]" if r["is_control"] else ""
        cr = f" risk={r['counter_risk_score']}" if r['counter_risk_score'] is not None else ""
        wl = f" wl={r['watchlist_users']}" if r['watchlist_users'] else ""
        oc = r['outcome_class'] or 'PENDING'
        c6 = f"{r['outcome_6h_change_pct']:+.1f}%" if r['outcome_6h_change_pct'] is not None else "-"
        c24 = f"{r['outcome_24h_change_pct']:+.1f}%" if r['outcome_24h_change_pct'] is not None else "-"
        c48 = f"{r['outcome_48h_change_pct']:+.1f}%" if r['outcome_48h_change_pct'] is not None else "-"
        pk = f" peak={r['peak_change_pct']:+.1f}%" if r['peak_change_pct'] is not None else ""
        print(f"  {r['symbol']} in {r['category_name']}{ctrl}")
        print(f"    fit={r['narrative_fit_score']} {r['confidence']}{cr}{wl} regime={r['market_regime']} trig={r['trigger_count']}")
        print(f"    6h={c6} 24h={c24} 48h={c48}{pk} -> {oc}")
        print(f"    at {r['predicted_at']}")

    # Hit rates
    evaluated = [r for r in agent_rows if r["outcome_class"] and r["outcome_class"] != "UNRESOLVED"]
    ctrl_eval = [r for r in ctrl_rows if r["outcome_class"] and r["outcome_class"] != "UNRESOLVED"]
    if evaluated:
        hits = sum(1 for r in evaluated if r["outcome_class"] == "HIT")
        ahr = hits / len(evaluated) * 100
        print(f"\n  Agent hit rate: {ahr:.1f}% ({hits}/{len(evaluated)})")
    if ctrl_eval:
        chits = sum(1 for r in ctrl_eval if r["outcome_class"] == "HIT")
        chr_ = chits / len(ctrl_eval) * 100
        print(f"  Control hit rate: {chr_:.1f}% ({chits}/{len(ctrl_eval)})")
        if evaluated:
            print(f"  TRUE ALPHA: {ahr - chr_:+.1f}pp")

    # Heating categories
    print("\n--- TOP HEATING CATEGORIES ---")
    async with conn.execute("""
        SELECT category_name, COUNT(*) as times, MAX(acceleration) as max_accel,
               MAX(trigger_count) as max_trig
        FROM narrative_signals GROUP BY category_name ORDER BY times DESC LIMIT 15
    """) as cur:
        rows = await cur.fetchall()
    for r in rows:
        print(f"  {r['category_name']}: {r['times']}x (accel={r['max_accel']:.1f}%, trig={r['max_trig']})")

    # Chain matches
    print("\n--- CHAIN MATCHES (last 10) ---")
    async with conn.execute("""
        SELECT pattern_id, pipeline, token_id, steps_matched, conviction_boost,
               outcome_class, completed_at
        FROM chain_matches ORDER BY completed_at DESC LIMIT 10
    """) as cur:
        rows = await cur.fetchall()
    for r in rows:
        oc = r['outcome_class'] or 'PENDING'
        print(f"  {r['pattern_id']} [{r['pipeline']}] {r['token_id']} steps={r['steps_matched']} boost={r['conviction_boost']} -> {oc}")

    # Signal events by type
    print("\n--- SIGNAL EVENTS BY TYPE ---")
    async with conn.execute("""
        SELECT event_type, pipeline, COUNT(*) as c
        FROM signal_events GROUP BY event_type, pipeline ORDER BY c DESC
    """) as cur:
        rows = await cur.fetchall()
    for r in rows:
        print(f"  {r['event_type']} [{r['pipeline']}]: {r['c']}")

    # Strategy changes
    print("\n--- STRATEGY (non-default values) ---")
    async with conn.execute("SELECT key, value, updated_by, reason FROM agent_strategy WHERE updated_by != 'init'") as cur:
        rows = await cur.fetchall()
    if rows:
        for r in rows:
            print(f"  {r['key']} = {r['value']} ({r['updated_by']}: {r['reason']})")
    else:
        print("  All at defaults — LEARN has not run yet")

    # Learn logs
    print("\n--- LEARN LOGS ---")
    async with conn.execute("SELECT cycle_number, cycle_type, hit_rate_before, reflection_text, created_at FROM learn_logs ORDER BY created_at DESC LIMIT 3") as cur:
        rows = await cur.fetchall()
    if rows:
        for r in rows:
            rt = (r['reflection_text'] or '')[:300]
            print(f"  Cycle {r['cycle_number']} ({r['cycle_type']}) HR={r['hit_rate_before']}% at {r['created_at']}")
            print(f"    {rt}")
    else:
        print("  No learn cycles yet")

    await db.close()

asyncio.run(report())
