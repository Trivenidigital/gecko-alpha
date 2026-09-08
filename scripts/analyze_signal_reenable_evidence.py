#!/usr/bin/env python3
"""Regenerate every figure in tasks/decision_signal_reenable_2026_09_08.md.

Committed because that document ORDERS a future re-derivation ("167 is stale
the moment the distribution moves"), and a re-derivation has to be comparable
to the original. A seed alone does not confer determinism: the RNG, the row
ordering that fixes the resample input, and the CI method all have to be
pinned too, or the "same" analysis run later is a different analysis.

DETERMINISM CONTRACT
  * stdlib `random.Random`, seeded PER CALL, never a shared module-level
    stream. A single `random.seed()` followed by several bootstrap loops
    advances the state between them, so two loops over the SAME sample
    produce DIFFERENT intervals. That defect produced two different CIs for
    chain_completed in the first revision of the decision document, and the
    difference happened to straddle the "excludes zero" boundary that a
    headline rested on.
  * rows ordered by `id` so the resample input is a fixed sequence.
  * percentile bootstrap, 10,000 resamples, 2.5/97.5.

Usage:  uv run python scripts/analyze_signal_reenable_evidence.py [db_path]
"""

from __future__ import annotations

import math
import random
import sqlite3
import statistics as st
import sys
from datetime import datetime, timezone

SIGNALS = ("chain_completed", "first_signal", "volume_spike")
SEED = 20260908
RESAMPLES = 10_000
# Only closed trades with a realised return. Stated here rather than inlined so
# the denominator question ("what did this filter remove?") is answerable.
CLOSED = "status LIKE 'closed%' AND pnl_pct IS NOT NULL"


def _rows(con, sig, where="", params=()):
    q = (f"SELECT pnl_pct FROM paper_trades WHERE signal_type = ? AND {CLOSED} "
         f"{where} ORDER BY id")
    return [r[0] for r in con.execute(q, (sig, *params))]


def _boot(sample, seed):
    """Percentile bootstrap. Fresh Random per call — see determinism contract."""
    rng = random.Random(seed)
    n = len(sample)
    means = sorted(
        sum(sample[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(RESAMPLES)
    )
    return means[int(0.025 * RESAMPLES)], means[int(0.975 * RESAMPLES)]


def main(db_path="scout.db"):
    con = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc)
    print(f"# regenerated {now.isoformat()}  db={db_path}  seed={SEED}  "
          f"resamples={RESAMPLES}")
    mx = con.execute("SELECT MAX(closed_at) FROM paper_trades").fetchone()[0]
    print(f"# max(closed_at) in paper_trades = {mx}   <- data provenance\n")

    print("## Lifetime")
    print(f"{'signal':17} {'n':>4} {'mean%':>7} {'95% CI':>20} {'net_usd':>9} {'WR%':>6}")
    for sig in SIGNALS:
        r = _rows(con, sig)
        lo, hi = _boot(r, SEED)
        net, wr = con.execute(
            f"SELECT ROUND(SUM(pnl_usd),0), ROUND(100.0*SUM(pnl_pct>0)/COUNT(*),1) "
            f"FROM paper_trades WHERE signal_type=? AND {CLOSED}", (sig,)).fetchone()
        print(f"{sig:17} {len(r):4d} {st.mean(r):7.2f}   [{lo:7.2f},{hi:7.2f}] "
              f"{net:9.0f} {wr:6.1f}")

    print("\n## Recent window — LAST 60 DAYS OF EACH SIGNAL'S OWN LIFE")
    print("## (NOT a common calendar window; the three cover different periods)")
    print(f"{'signal':17} {'n':>4} {'mean%':>7} {'95% CI':>20}  window")
    for sig in SIGNALS:
        b = con.execute(
            "SELECT date(MAX(opened_at),'-60 day'), date(MAX(opened_at)) "
            "FROM paper_trades WHERE signal_type=?", (sig,)).fetchone()
        r = _rows(con, sig, "AND opened_at >= ?", (b[0],))
        if len(r) < 5:
            print(f"{sig:17} {len(r):4d}  INSUFFICIENT_DATA")
            continue
        lo, hi = _boot(r, SEED)
        print(f"{sig:17} {len(r):4d} {st.mean(r):7.2f}   [{lo:7.2f},{hi:7.2f}]  "
              f"{b[0]} .. {b[1]}")

    print("\n## Censoring — UNFILTERED denominator (the filter's own removals)")
    print(f"{'signal':17} {'all_rows':>9} {'closed+pnl':>11} {'open':>5} {'other':>6}")
    for sig in SIGNALS:
        tot = con.execute("SELECT COUNT(*) FROM paper_trades WHERE signal_type=?",
                          (sig,)).fetchone()[0]
        cl = con.execute(f"SELECT COUNT(*) FROM paper_trades WHERE signal_type=? "
                         f"AND {CLOSED}", (sig,)).fetchone()[0]
        op = con.execute("SELECT COUNT(*) FROM paper_trades WHERE signal_type=? "
                         "AND status='open'", (sig,)).fetchone()[0]
        print(f"{sig:17} {tot:9d} {cl:11d} {op:5d} {tot-cl-op:6d}")
    print("# NOTE: this bounds POST-OPEN censoring only. It cannot see candidates"
          "\n# filtered before a trade was ever opened.")

    print("\n## Small-sample width at n=5 (normal vs t, df=4)")
    print(f"{'signal':17} {'SD%':>7} {'z half':>8} {'t half':>8}")
    for sig in SIGNALS:
        sd = st.stdev(_rows(con, sig))
        print(f"{sig:17} {sd:7.2f} {1.96*sd/math.sqrt(5):7.1f}% "
              f"{2.776*sd/math.sqrt(5):7.1f}%")

    print("\n## Sample needed to resolve +/-2 percentage points")
    for sig in SIGNALS:
        r = _rows(con, sig)
        sd = st.stdev(r)
        n_need = math.ceil((1.96 * sd / 2.0) ** 2)
        span = con.execute(
            "SELECT julianday(MAX(opened_at))-julianday(MIN(opened_at)) "
            "FROM paper_trades WHERE signal_type=?", (sig,)).fetchone()[0] or 1
        rate = len(r) / max(span, 1)
        print(f"  {sig:17} n={n_need:5d}  at {rate:.2f}/day -> {n_need/rate:.0f} days")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "scout.db")
