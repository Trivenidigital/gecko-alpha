"""Backtest v1 — research for BL-067 + BL-068.

Three deliverables, all read-only against scout.db:

A. Conviction-stacking prevalence
   For every paper trade in the last 30d, count distinct independent signals
   that fired on the same token between opened_at and closed_at. Compare
   actual outcome vs the stack count.

B. chain_completed buy-and-hold simulation
   For every chain_match in the last 30d, treat it as a hypothetical paper
   trade opened at completion-price and held to now (or to outcome window
   if `outcome_change_pct` is populated). Sum simulated PnL.

C. Screenshot-token case studies
   For NOCK/CHIP/ORCA/BLEND/ZKJ/MAGA/OPG/NAORIS/ZBCN/SKR/UPEG/SWARMS/TOSHI/LA,
   list all paper trades, all signals, and the hypothetical hold-to-now
   outcome.

Run on VPS:
    cd /root/gecko-alpha && uv run python scripts/backtest_v1_signal_stacking.py

No production changes. Pure analysis. If results are compelling, BL-067/068
move from research-gated to ready-to-build.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path("scout.db")
TRADE_SIZE_USD = 300.0  # PAPER_TG_SOCIAL_TRADE_AMOUNT_USD baseline


def _h(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# Section A — Conviction-stacking prevalence
# ---------------------------------------------------------------------------

# Each tuple is (table, ts_column, label)
# Note: gainers_snapshots fires every ~80s while a token is on the leaderboard.
# To avoid every gainers row counting as a separate "stack signal", we count
# only the FIRST appearance per source per trade window.
_SIGNAL_SOURCES = [
    ("gainers_snapshots", "snapshot_at", "gainers"),
    ("losers_snapshots", "snapshot_at", "losers"),
    ("trending_snapshots", "snapshot_at", "trending"),
    ("chain_matches", "completed_at", "chains"),
]
# narrative_predictions and velocity_alerts handled separately below
# because column names diverge.


def _distinct_stack_count(conn, token_id: str, opened_at: str, end_at: str) -> tuple[int, list[str]]:
    """Count DISTINCT signal-source firings on token_id within the window.

    "Distinct" means: each source contributes at most 1 to the stack count.
    A token grinding on gainers for 100 ticks counts as 1 'gainers' stack,
    not 100. This is what the BIO case study showed mattered: signal class
    diversity, not signal-event volume.
    """
    sources: list[str] = []
    for table, ts_col, label in _SIGNAL_SOURCES:
        token_col = "token_id" if table == "chain_matches" else "coin_id"
        cur = conn.execute(
            f"""SELECT 1 FROM {table}
                WHERE {token_col} = ?
                  AND datetime({ts_col}) >= datetime(?)
                  AND datetime({ts_col}) <= datetime(?)
                LIMIT 1""",
            (token_id, opened_at, end_at),
        )
        if cur.fetchone() is not None:
            sources.append(label)

    # narrative_predictions
    cur = conn.execute(
        """SELECT 1 FROM narrative_predictions
           WHERE coin_id = ?
             AND datetime(created_at) >= datetime(?)
             AND datetime(created_at) <= datetime(?)
           LIMIT 1""",
        (token_id, opened_at, end_at),
    )
    if cur.fetchone() is not None:
        sources.append("narrative")

    # velocity_alerts — column may not exist on this branch; guard
    try:
        cur = conn.execute(
            """SELECT 1 FROM velocity_alerts
               WHERE coin_id = ?
                 AND datetime(detected_at) >= datetime(?)
                 AND datetime(detected_at) <= datetime(?)
               LIMIT 1""",
            (token_id, opened_at, end_at),
        )
        if cur.fetchone() is not None:
            sources.append("velocity")
    except sqlite3.OperationalError:
        pass

    # volume_spikes
    try:
        cur = conn.execute(
            """SELECT 1 FROM volume_spikes
               WHERE coin_id = ?
                 AND datetime(detected_at) >= datetime(?)
                 AND datetime(detected_at) <= datetime(?)
               LIMIT 1""",
            (token_id, opened_at, end_at),
        )
        if cur.fetchone() is not None:
            sources.append("volume_spike")
    except sqlite3.OperationalError:
        pass

    # tg_social_signals
    cur = conn.execute(
        """SELECT 1 FROM tg_social_signals
           WHERE token_id = ?
             AND datetime(created_at) >= datetime(?)
             AND datetime(created_at) <= datetime(?)
           LIMIT 1""",
        (token_id, opened_at, end_at),
    )
    if cur.fetchone() is not None:
        sources.append("tg_social")

    # Other paper_trade signal_types on the same token (independent confirmation)
    cur = conn.execute(
        """SELECT DISTINCT signal_type FROM paper_trades
           WHERE token_id = ?
             AND datetime(opened_at) >= datetime(?)
             AND datetime(opened_at) <= datetime(?)""",
        (token_id, opened_at, end_at),
    )
    other_signal_types = {r[0] for r in cur.fetchall()}
    for st in other_signal_types:
        sources.append(f"trade:{st}")

    return len(sources), sources


def section_a(conn) -> None:
    _h("SECTION A — Conviction-stacking prevalence (last 30d)")

    cur = conn.execute(
        """SELECT id, token_id, signal_type, status, opened_at, closed_at,
                  pnl_usd, pnl_pct, peak_pct, exit_reason
           FROM paper_trades
           WHERE datetime(opened_at) >= datetime('now','-30 days')
           ORDER BY opened_at"""
    )
    trades = cur.fetchall()
    print(f"Total trades in window: {len(trades)}")

    by_stack: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for t in trades:
        end_at = t["closed_at"] or "now"
        if end_at == "now":
            end_at_lit = "datetime('now')"
            count_query_end = None
        # We need the actual end timestamp string. Use closed_at if present.
        end_ts = t["closed_at"]
        if end_ts is None:
            cur2 = conn.execute("SELECT datetime('now')")
            end_ts = cur2.fetchone()[0]
        n, _sources = _distinct_stack_count(conn, t["token_id"], t["opened_at"], end_ts)
        by_stack[n].append(t)

    print()
    print(f"{'Stack count':<14} {'n trades':<10} {'avg pnl_usd':<14} "
          f"{'avg peak_pct':<14} {'avg capture_pct':<16} "
          f"{'win_rate':<10} {'expired_pct':<12}")
    print("-" * 90)

    for stack in sorted(by_stack.keys()):
        ts = by_stack[stack]
        closed = [t for t in ts if t["status"] and t["status"].startswith("closed_")]
        if not closed:
            continue
        n = len(closed)
        avg_pnl = sum(t["pnl_usd"] or 0 for t in closed) / n
        avg_peak = sum(t["peak_pct"] or 0 for t in closed) / n
        avg_cap = sum(t["pnl_pct"] or 0 for t in closed) / n
        wins = sum(1 for t in closed if (t["pnl_usd"] or 0) > 0)
        expired = sum(
            1 for t in closed if t["status"] in ("closed_expired", "closed_expired_stale_price")
        )
        print(
            f"{stack:<14} {n:<10} ${avg_pnl:>10.2f}    "
            f"{avg_peak:>10.2f}%    {avg_cap:>10.2f}%       "
            f"{100*wins/n:>5.1f}%     {100*expired/n:>5.1f}%"
        )

    # Aggregate net pnl by stack >= 2 vs stack < 2
    low = [t for ts in [by_stack[k] for k in by_stack if k < 2] for t in ts]
    high = [t for ts in [by_stack[k] for k in by_stack if k >= 2] for t in ts]
    low_closed = [t for t in low if t["status"] and t["status"].startswith("closed_")]
    high_closed = [t for t in high if t["status"] and t["status"].startswith("closed_")]
    print()
    print(f"Stack <2:  n={len(low_closed):>4d}  net=${sum(t['pnl_usd'] or 0 for t in low_closed):>10.2f}")
    print(f"Stack >=2: n={len(high_closed):>4d}  net=${sum(t['pnl_usd'] or 0 for t in high_closed):>10.2f}")


# ---------------------------------------------------------------------------
# Section B — chain_completed buy-and-hold simulation
# ---------------------------------------------------------------------------


def section_b(conn) -> None:
    _h("SECTION B — chain_completed buy-and-hold (last 30d)")

    cur = conn.execute(
        """SELECT cm.id, cm.token_id, cm.pattern_name, cm.completed_at,
                  cm.outcome_change_pct, cm.outcome_class, cm.conviction_boost,
                  pc.current_price AS now_price
           FROM chain_matches cm
           LEFT JOIN price_cache pc ON pc.coin_id = cm.token_id
           WHERE datetime(cm.completed_at) >= datetime('now','-30 days')
           ORDER BY cm.completed_at"""
    )
    matches = cur.fetchall()
    print(f"chain_matches in window: {len(matches)}")
    if not matches:
        return

    # outcome_change_pct path — what the chain-evaluator already recorded
    eval_pct = [m["outcome_change_pct"] for m in matches if m["outcome_change_pct"] is not None]
    print()
    print(f"With recorded outcome_change_pct: {len(eval_pct)} / {len(matches)}")
    if eval_pct:
        avg = sum(eval_pct) / len(eval_pct)
        wins = sum(1 for p in eval_pct if p > 0)
        big_wins = sum(1 for p in eval_pct if p > 50)
        sim_pnl_each = [TRADE_SIZE_USD * p / 100.0 for p in eval_pct]
        print(f"  avg outcome %    : {avg:>7.2f}%")
        print(f"  win rate         : {100*wins/len(eval_pct):>5.1f}%")
        print(f"  >+50% outcomes   : {big_wins} ({100*big_wins/len(eval_pct):.1f}%)")
        print(f"  simulated $/trade: ${sum(sim_pnl_each)/len(sim_pnl_each):>7.2f}")
        print(f"  simulated NET pnl: ${sum(sim_pnl_each):>10.2f}  (over {len(eval_pct)} trades, ${TRADE_SIZE_USD:.0f}/trade)")

    # outcome_class breakdown
    classes: dict[str, int] = defaultdict(int)
    for m in matches:
        classes[m["outcome_class"] or "unevaluated"] += 1
    print()
    print("outcome_class breakdown:")
    for k, v in sorted(classes.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<25} {v}")

    # by-pattern view
    print()
    print("By pattern_name:")
    cur = conn.execute(
        """SELECT pattern_name,
                  COUNT(*) AS n,
                  AVG(outcome_change_pct) AS avg_pct,
                  SUM(CASE WHEN outcome_change_pct > 0 THEN 1 ELSE 0 END) AS wins
           FROM chain_matches
           WHERE datetime(completed_at) >= datetime('now','-30 days')
             AND outcome_change_pct IS NOT NULL
           GROUP BY pattern_name
           ORDER BY avg_pct DESC"""
    )
    print(f"  {'pattern':<35} {'n':>4} {'avg %':>8} {'win %':>8}")
    print("  " + "-" * 60)
    for r in cur.fetchall():
        wp = 100 * (r["wins"] or 0) / max(1, r["n"])
        print(f"  {r['pattern_name']:<35} {r['n']:>4} {r['avg_pct']:>7.2f}% {wp:>7.1f}%")

    # Hold-to-now simulation (where we have current price)
    print()
    print("Hold-to-now simulation (uses price_cache.current_price):")
    held = []
    for m in matches:
        if m["now_price"] is None:
            continue
        # Use first price snapshot at/near completed_at as entry
        entry_cur = conn.execute(
            """SELECT price_at_snapshot FROM gainers_snapshots
               WHERE coin_id = ?
                 AND datetime(snapshot_at) >= datetime(?, '-1 hour')
                 AND datetime(snapshot_at) <= datetime(?, '+1 hour')
                 AND price_at_snapshot > 0
               ORDER BY ABS(julianday(snapshot_at) - julianday(?))
               LIMIT 1""",
            (m["token_id"], m["completed_at"], m["completed_at"], m["completed_at"]),
        )
        row = entry_cur.fetchone()
        if row is None:
            continue
        entry = float(row[0])
        nowp = float(m["now_price"])
        pct = (nowp - entry) / entry * 100
        held.append((m["token_id"], pct))

    if held:
        held_sorted = sorted(held, key=lambda x: -x[1])
        sum_pct = sum(p for _, p in held)
        avg_pct = sum_pct / len(held)
        wins = sum(1 for _, p in held if p > 0)
        sim = sum(TRADE_SIZE_USD * p / 100.0 for _, p in held)
        print(f"  matches with usable entry price : {len(held)}")
        print(f"  avg % held-to-now              : {avg_pct:>7.2f}%")
        print(f"  win rate                       : {100*wins/len(held):>5.1f}%")
        print(f"  simulated NET (${TRADE_SIZE_USD:.0f}/trade)        : ${sim:>10.2f}")
        print()
        print("  Top 5 held-to-now winners:")
        for tok, p in held_sorted[:5]:
            print(f"    {tok:<28} {p:>+7.1f}%")
        print("  Bottom 5 held-to-now losers:")
        for tok, p in held_sorted[-5:]:
            print(f"    {tok:<28} {p:>+7.1f}%")


# ---------------------------------------------------------------------------
# Section C — Screenshot-token case studies
# ---------------------------------------------------------------------------

_SCREEN_TOKENS = ["nock", "chip", "orca", "blend", "zkj", "maga", "opg",
                  "naoris", "zbcn", "skr", "upeg", "swarms", "toshi", "la"]


def section_c(conn) -> None:
    _h("SECTION C — Screenshot tokens — actual vs hypothetical hold-to-now")

    print(f"{'Token':<10} {'Trades':<8} {'Best $':<10} {'Worst $':<10} "
          f"{'Net $':<10} {'1st sig peak %':<16} "
          f"{'Hold-to-now %':<16} {'Hypothetical $':<16}")
    print("-" * 110)

    for sym in _SCREEN_TOKENS:
        cur = conn.execute(
            """SELECT signal_type, status, opened_at, closed_at, pnl_usd,
                      peak_pct, entry_price, token_id
               FROM paper_trades
               WHERE LOWER(symbol) = ?
               ORDER BY opened_at""",
            (sym,),
        )
        trades = cur.fetchall()
        if not trades:
            print(f"{sym.upper():<10} (no paper trades)")
            continue

        token_id = trades[0]["token_id"]
        first_entry = float(trades[0]["entry_price"]) if trades[0]["entry_price"] else None

        pnls = [t["pnl_usd"] or 0 for t in trades if t["status"] and t["status"].startswith("closed_")]
        n = len(trades)
        best = max(pnls) if pnls else 0
        worst = min(pnls) if pnls else 0
        net = sum(pnls)
        first_peak = trades[0]["peak_pct"] or 0

        # Hold-to-now: first entry → current price
        cur = conn.execute(
            "SELECT current_price FROM price_cache WHERE coin_id = ?",
            (token_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] is None or first_entry is None or first_entry <= 0:
            held_pct = None
            hypo_usd = None
        else:
            held_pct = (float(row[0]) - first_entry) / first_entry * 100
            hypo_usd = TRADE_SIZE_USD * held_pct / 100.0

        held_str = f"{held_pct:+.1f}%" if held_pct is not None else "n/a"
        hypo_str = f"${hypo_usd:+.0f}" if hypo_usd is not None else "n/a"
        print(
            f"{sym.upper():<10} {n:<8} ${best:>+7.0f}   ${worst:>+7.0f}   "
            f"${net:>+7.0f}    {first_peak:>+10.1f}%      "
            f"{held_str:<16} {hypo_str}"
        )


# ---------------------------------------------------------------------------
# Headline summary
# ---------------------------------------------------------------------------


def headline(conn) -> None:
    _h("HEADLINE")
    cur = conn.execute(
        """SELECT
             COUNT(*) AS n,
             SUM(pnl_usd) AS net,
             SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins
           FROM paper_trades
           WHERE status LIKE 'closed_%'
             AND datetime(closed_at) >= datetime('now','-30 days')"""
    )
    r = cur.fetchone()
    print(f"Actual paper trades 30d: n={r['n']}, net=${r['net']:.2f}, "
          f"win rate={100*r['wins']/max(1,r['n']):.1f}%")


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Run from /root/gecko-alpha — {DB_PATH} not found")
    with _conn() as c:
        headline(c)
        section_a(c)
        section_b(c)
        section_c(c)


if __name__ == "__main__":
    main()
