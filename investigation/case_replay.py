#!/usr/bin/env python3
"""investigation/case_replay.py — READ-ONLY per-token forensic replay.

Standalone (stdlib sqlite3 only). For each token id/symbol given, answers the
Phase-3 questions from the DB:
  1. ever ingested?      (candidates, gainers/trending/losers snapshots, price_cache)
  2. signals + score     (candidates.quant_score / signals_fired / conviction)
  3. killed-at stage     (alerted_at NULL vs set; tg_alert_log outcomes; suppression)
  4. earliest possible   (first_seen_at vs first gainers/trending appearance)
  5. hypothetical PnL    (paper_trades row if the paper engine opened one)

Usage (VPS):
  python3 investigation/case_replay.py --db /root/gecko-alpha/scout.db \
      ansem catcash jotchua <coin_id_or_symbol> ...
Symbols are matched case-insensitively against candidates.ticker,
gainers_snapshots.symbol, trending_snapshots (coin_id LIKE) and price_cache.

Evidence discipline: if ANY of the per-token queries fails (missing table,
renamed column, incompatible schema), the verdict is
``indeterminate_query_error`` carrying the exact error(s) — a query failure
must NEVER be classified as seen/unseen/alerted. Exit code is 3 when any
token's replay was indeterminate, 0 otherwise.
"""

import argparse
import json
import sqlite3
import sys


def ro(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def q1(conn, sql, args):
    try:
        return conn.execute(sql, args).fetchone()
    except sqlite3.OperationalError as e:
        return {"error": str(e)}


def replay(conn, key):
    k = key.lower()
    like = f"%{k}%"
    out = {"query": key}

    out["candidate"] = q1(
        conn,
        """
        SELECT contract_address, chain, ticker, first_seen_at, quant_score,
               conviction_score, signals_fired, alerted_at, market_cap_usd
        FROM candidates
        WHERE lower(contract_address)=? OR lower(ticker)=? OR lower(token_name) LIKE ?
        ORDER BY first_seen_at LIMIT 1""",
        (k, k, like),
    )

    out["first_gainers_seen"] = q1(
        conn,
        """
        SELECT coin_id, symbol, MIN(snapshot_at) first_seen,
               MAX(price_change_24h) best_24h
        FROM gainers_snapshots
        WHERE lower(coin_id)=? OR lower(symbol)=? OR lower(name) LIKE ?
        GROUP BY coin_id LIMIT 1""",
        (k, k, like),
    )

    out["first_trending_seen"] = q1(
        conn,
        """
        SELECT coin_id, MIN(snapshot_at) first_seen FROM trending_snapshots
        WHERE lower(coin_id) LIKE ? GROUP BY coin_id LIMIT 1""",
        (like,),
    )

    out["comparisons"] = q1(
        conn,
        """
        SELECT coin_id, appeared_on_gainers_at, detected_by_pipeline,
               pipeline_lead_minutes, detected_by_narrative, narrative_lead_minutes
        FROM gainers_comparisons
        WHERE lower(coin_id)=? OR lower(symbol)=? LIMIT 1""",
        (k, k),
    )

    out["tg_alert_log"] = q1(
        conn,
        """
        SELECT COUNT(*) n, SUM(outcome='sent') sent,
               GROUP_CONCAT(DISTINCT outcome) outcomes
        FROM tg_alert_log WHERE lower(token_id)=?""",
        (k,),
    )

    out["paper_trade"] = q1(
        conn,
        """
        SELECT signal_type, opened_at, entry_price, status, exit_reason,
               pnl_pct, peak_pct, checkpoint_24h_pct
        FROM paper_trades WHERE lower(token_id)=? OR lower(symbol)=?
        ORDER BY opened_at LIMIT 1""",
        (k, k),
    )

    out["price_cache"] = q1(
        conn,
        """
        SELECT coin_id, current_price, market_cap, updated_at FROM price_cache
        WHERE lower(coin_id)=? OR lower(coin_id) LIKE ? LIMIT 1""",
        (k, like),
    )

    # Evidence gate BEFORE any classification: a query that errored (missing
    # table / renamed column / incompatible schema) means the evidence is
    # incomplete — the verdict must be indeterminate, never a business
    # classification synthesized from partial data.
    query_errors = {
        field: value["error"]
        for field, value in out.items()
        if isinstance(value, dict) and "error" in value
    }
    if query_errors:
        out["verdict"] = "indeterminate_query_error"
        out["query_errors"] = query_errors
        return out

    # Verdict skeleton the analyst fills: seen? -> stage killed
    cand = out["candidate"]
    if (
        cand is None
        and out["first_gainers_seen"] is None
        and out["first_trending_seen"] is None
        and out["price_cache"] is None
    ):
        out["verdict"] = "NEVER SEEN by any table — source-coverage gap (H4)"
    elif cand is None:
        out["verdict"] = (
            "seen by snapshots but never became a candidate — ingestion filter"
        )
    elif cand["alerted_at"] is None:
        sf = cand["signals_fired"]
        out["verdict"] = (
            f"candidate, quant={cand['quant_score']} conv={cand['conviction_score']} "
            f"signals={sf} — never alerted (check gate threshold vs conv, "
            "suppression state, dispatch outcomes above)"
        )
    else:
        out["verdict"] = "alerted — compare alert time vs pump start"
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("tokens", nargs="+")
    args = ap.parse_args(argv)
    conn = ro(args.db)
    results = []
    for t in args.tokens:
        r = replay(conn, t)
        results.append(
            {kk: (dict(v) if isinstance(v, sqlite3.Row) else v) for kk, v in r.items()}
        )
    json.dump(results, sys.stdout, indent=2, default=str)
    print()
    # Exit 3 when any replay was evidence-incomplete, so automation cannot
    # mistake an indeterminate run for a clean forensic result.
    if any(r.get("verdict") == "indeterminate_query_error" for r in results):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
