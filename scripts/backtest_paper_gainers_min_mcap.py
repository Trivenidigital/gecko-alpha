"""Path-dependent backtest for PAPER_GAINERS_MIN_MCAP=3M proposal.

Replaces the methodologically-unsound MAX/MIN aggregate simulation in
tasks/plan_paper_gainers_min_mcap_3m.md §3 (CRITICAL finding STAT-C1, PR #83).

For each candidate that would have been newly eligible at $3M floor, walks
forward through prod price snapshots in chronological order and applies the
gainers_early ladder rules in the order they would actually fire (matching
scout/trading/evaluator.py BL-061 cascade: SL → Leg 1 → Leg 2 → Floor → Trail):

  - SL fires only BEFORE leg_1 (not floor_armed): first snapshot with
    (price/entry - 1) <= -sl_pct (default -25%) → close entire position
  - leg_1 on first snapshot where (price/entry - 1) >= leg_1_pct (default +10%) → close 50%, ARM FLOOR
  - leg_2 on first snapshot where (price/entry - 1) >= leg_2_pct (default +50%) → close 30%
  - Floor (post-leg_1): if current_price <= entry_price → close runner at current price
  - Trail (post-leg_1, peak >= leg_1_pct): drawdown_from_peak >= effective_trail
      - trail_pct=20 if peak_pct >= 20 (low_peak_threshold_pct)
      - trail_pct_low_peak=8 otherwise
  - max_duration: forced exit at last snapshot in 168h window if not closed

Critical: trail does NOT fire before leg_1 fills. Pre-leg_1, only SL or
max_duration can close the position.

A coin that drops -25% before peaking +75% will SL out at -25% — the broken
aggregate model would have banked the +75% peak.

## Inputs

Three CSVs in cwd (no header rows):
  - .candidates.csv: coin_id,symbol,entry_at,entry_pct,entry_mc,entry_price
  - .snaps_g.csv: coin_id,snapshot_at,price,market_cap,price_change_24h
  - .snaps_v.csv: coin_id,recorded_at,price,market_cap,(empty)

Refresh from prod:
  ssh srilu-vps 'sqlite3 -separator "," /root/gecko-alpha/scout.db "..."' > .candidates.csv
  ...etc (see plan_paper_gainers_min_mcap_3m.md resume checklist for queries)

## Output

Markdown table per coin: status, exit_pct, gross_pnl_at_300, ladder fill log.
Plus aggregate: strike rate, SL ratio, mean PnL/trade, total PnL.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

# Ladder params from prod signal_params (gainers_early row, 2026-05-08)
LEG_1_PCT = 10.0
LEG_1_QTY_FRAC = 0.5
LEG_2_PCT = 50.0
LEG_2_QTY_FRAC = 0.3
TRAIL_PCT = 20.0
TRAIL_PCT_LOW_PEAK = 8.0
LOW_PEAK_THRESHOLD_PCT = 20.0
SL_PCT = 25.0
TRADE_USD = 300.0  # default size used in spec projections


@dataclass
class TradeOutcome:
    coin_id: str
    symbol: str
    entry_pct: float
    entry_mc_M: float
    entry_price: float
    status: str  # closed_sl | closed_max_duration | closed_trail | open
    exit_pct: float  # final realized peak vs entry, weighted across legs
    peak_pct: float
    min_pct_pre_peak: float
    leg_1_filled: bool
    leg_2_filled: bool
    trail_exit_pct: float | None  # pct at trail exit on the runner slice
    gross_pnl: float
    fill_log: list[str] = field(default_factory=list)


def load_candidates(path: str = ".candidates.csv") -> list[dict]:
    out = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if not row or len(row) < 6:
                continue
            out.append(
                {
                    "coin_id": row[0],
                    "symbol": row[1],
                    "entry_at": row[2],
                    "entry_pct": float(row[3]),
                    "entry_mc": float(row[4]),
                    "entry_price": float(row[5]),
                }
            )
    return out


def load_snapshots(coin_id: str) -> list[tuple[str, float]]:
    """Load (timestamp, price) pairs for one coin from both CSVs, union + dedupe."""
    rows: dict[str, float] = {}
    for path in (".snaps_g.csv", ".snaps_v.csv"):
        if not Path(path).exists():
            continue
        with open(path, newline="") as f:
            for row in csv.reader(f):
                if not row or len(row) < 3 or row[0] != coin_id:
                    continue
                ts = row[1]
                try:
                    price = float(row[2])
                except (ValueError, IndexError):
                    continue
                if price <= 0:
                    continue
                # Dedupe — gainers and volume tables can have near-duplicate rows
                # at the same minute. Keep first.
                if ts not in rows:
                    rows[ts] = price
    return sorted(rows.items())


def simulate(c: dict) -> TradeOutcome:
    """Walk forward through snapshots and apply ladder."""
    entry_price = c["entry_price"]
    entry_at = c["entry_at"]

    snaps = load_snapshots(c["coin_id"])
    snaps = [(ts, p) for ts, p in snaps if ts >= entry_at]

    out = TradeOutcome(
        coin_id=c["coin_id"],
        symbol=c["symbol"],
        entry_pct=c["entry_pct"],
        entry_mc_M=c["entry_mc"] / 1e6,
        entry_price=entry_price,
        status="open",
        exit_pct=0.0,
        peak_pct=0.0,
        min_pct_pre_peak=0.0,
        leg_1_filled=False,
        leg_2_filled=False,
        trail_exit_pct=None,
        gross_pnl=0.0,
    )

    if not snaps:
        out.status = "no_data"
        return out

    peak_pct = 0.0
    runner_qty_frac = 1.0  # remaining fraction of position
    realized_pct_weighted = 0.0  # sum of (qty_frac × exit_pct) across closed legs
    min_pct_pre_peak = 0.0
    floor_armed = False  # set True once leg_1 fills

    for ts, price in snaps:
        pct = (price / entry_price - 1.0) * 100.0

        # Track min before peak (for diagnostics)
        if pct < min_pct_pre_peak and peak_pct < LEG_2_PCT:
            min_pct_pre_peak = pct

        # ---- BL-061 cascade order: SL → Leg 1 → Leg 2 → Floor → Trail ----

        # SL — fires only BEFORE leg_1 (not floor_armed). Once leg_1 fills,
        # the floor takes over as the protective gate.
        if not floor_armed and pct <= -SL_PCT:
            out.status = "closed_sl"
            out.exit_pct = -SL_PCT
            out.fill_log.append(f"  SL @ {pct:.1f}% (ts={ts}) — entire position closed")
            realized_pct_weighted = -SL_PCT
            runner_qty_frac = 0.0
            break

        # leg_1 fires ONCE; arms the floor
        if not out.leg_1_filled and pct >= LEG_1_PCT:
            out.leg_1_filled = True
            floor_armed = True
            realized_pct_weighted += LEG_1_QTY_FRAC * LEG_1_PCT
            runner_qty_frac -= LEG_1_QTY_FRAC
            out.fill_log.append(
                f"  leg_1 fill @ +{LEG_1_PCT:.0f}% (ts={ts}) — closed {LEG_1_QTY_FRAC*100:.0f}%, floor_armed"
            )

        # leg_2 fires ONCE
        if not out.leg_2_filled and out.leg_1_filled and pct >= LEG_2_PCT:
            out.leg_2_filled = True
            realized_pct_weighted += LEG_2_QTY_FRAC * LEG_2_PCT
            runner_qty_frac -= LEG_2_QTY_FRAC
            out.fill_log.append(
                f"  leg_2 fill @ +{LEG_2_PCT:.0f}% (ts={ts}) — closed {LEG_2_QTY_FRAC*100:.0f}%"
            )

        # Update peak after leg processing
        if pct > peak_pct:
            peak_pct = pct

        # Floor (post-leg_1): close runner if price <= entry. Closes AT current price.
        if floor_armed and runner_qty_frac > 0 and pct <= 0:
            out.exit_pct = pct
            realized_pct_weighted += runner_qty_frac * pct
            out.fill_log.append(
                f"  floor exit @ {pct:.1f}% (ts={ts}) — runner closed at current price"
            )
            runner_qty_frac = 0.0
            out.status = "closed_floor"
            break

        # Trail (post-leg_1 only; peak must have reached leg_1_pct, which is
        # guaranteed by floor_armed since leg_1 fired at +leg_1_pct).
        if floor_armed and runner_qty_frac > 0 and peak_pct >= LEG_1_PCT:
            trail = TRAIL_PCT if peak_pct >= LOW_PEAK_THRESHOLD_PCT else TRAIL_PCT_LOW_PEAK
            drawdown_from_peak = peak_pct - pct
            if drawdown_from_peak >= trail:
                out.trail_exit_pct = pct
                realized_pct_weighted += runner_qty_frac * pct
                out.fill_log.append(
                    f"  trail exit @ {pct:.1f}% (ts={ts}, peak={peak_pct:.1f}%, drawdown={drawdown_from_peak:.1f}% >= {trail:.0f}%) — runner closed"
                )
                runner_qty_frac = 0.0
                out.status = "closed_trail"
                break

    # If still open at end of window, force-close at last price (max_duration)
    if runner_qty_frac > 0 and snaps:
        last_ts, last_price = snaps[-1]
        last_pct = (last_price / entry_price - 1.0) * 100.0
        realized_pct_weighted += runner_qty_frac * last_pct
        out.fill_log.append(
            f"  max_duration close @ {last_pct:.1f}% (ts={last_ts}) — runner force-closed"
        )
        out.status = "closed_max_duration" if out.status == "open" else out.status

    out.peak_pct = peak_pct
    out.min_pct_pre_peak = min_pct_pre_peak
    out.exit_pct = realized_pct_weighted
    out.gross_pnl = TRADE_USD * realized_pct_weighted / 100.0
    return out


def main() -> None:
    candidates = load_candidates()
    outcomes = [simulate(c) for c in candidates]

    print(f"# Path-dependent backtest — n={len(outcomes)}\n")
    print(f"Trade size: ${TRADE_USD:.0f}")
    print(
        f"Ladder: leg_1=+{LEG_1_PCT:.0f}%/{LEG_1_QTY_FRAC*100:.0f}%, "
        f"leg_2=+{LEG_2_PCT:.0f}%/{LEG_2_QTY_FRAC*100:.0f}%, "
        f"trail={TRAIL_PCT:.0f}% (low_peak={TRAIL_PCT_LOW_PEAK:.0f}% if peak<{LOW_PEAK_THRESHOLD_PCT:.0f}%), "
        f"sl={SL_PCT:.0f}%\n"
    )

    print("## Per-coin outcomes\n")
    print(
        "| coin_id | symbol | entry_pct | entry_mc_M | peak_pct | min_pre_peak | status | exit_pct | gross_pnl | leg_1 | leg_2 |"
    )
    print(
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for o in outcomes:
        print(
            f"| {o.coin_id} | {o.symbol} | {o.entry_pct:.1f}% | {o.entry_mc_M:.1f} | "
            f"{o.peak_pct:.1f}% | {o.min_pct_pre_peak:.1f}% | {o.status} | "
            f"{o.exit_pct:+.2f}% | ${o.gross_pnl:+.2f} | "
            f"{'Y' if o.leg_1_filled else '-'} | {'Y' if o.leg_2_filled else '-'} |"
        )

    print("\n## Fill logs (winners + SL hits)\n")
    for o in outcomes:
        if o.status == "closed_sl" or o.peak_pct >= 20.0:
            print(f"\n**{o.coin_id} ({o.symbol})** — {o.status}, peak {o.peak_pct:.1f}%, exit {o.exit_pct:+.2f}%")
            for line in o.fill_log:
                print(line)

    # Aggregates
    n = len(outcomes)
    n_closed = sum(1 for o in outcomes if o.status.startswith("closed"))
    n_strike_20 = sum(1 for o in outcomes if o.peak_pct >= 20.0)
    n_sl = sum(1 for o in outcomes if o.status == "closed_sl")
    n_negative = sum(1 for o in outcomes if o.gross_pnl < 0)
    total_pnl = sum(o.gross_pnl for o in outcomes)
    mean_pnl = total_pnl / n if n else 0.0

    print(f"\n## Aggregate stats (path-dependent)\n")
    print(f"- n total: {n}")
    print(f"- n closed: {n_closed}")
    print(f"- Strike rate (peak >= 20%): {n_strike_20}/{n} = {100*n_strike_20/n:.1f}%")
    print(f"- SL hits: {n_sl}/{n} = {100*n_sl/n:.1f}%")
    print(f"- Negative gross PnL: {n_negative}/{n} = {100*n_negative/n:.1f}%")
    print(f"- Mean gross PnL/trade: ${mean_pnl:+.2f}")
    print(f"- Total gross PnL (30d, n={n}): ${total_pnl:+.2f}")
    print(f"- Annualized at $300 sizing: ${total_pnl * 365 / 30:+.0f}/yr")

    # Wilson 95% CI for strike rate
    if n > 0:
        p = n_strike_20 / n
        z = 1.96
        wilson_center = (p + z * z / (2 * n)) / (1 + z * z / n)
        wilson_half = (
            z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
        )
        print(
            f"- Strike rate Wilson 95% CI: [{100*max(0, wilson_center - wilson_half):.1f}%, "
            f"{100*min(1, wilson_center + wilson_half):.1f}%]"
        )

    # Bootstrap 95% CI on total PnL (10000 resamples)
    import random

    pnls = [o.gross_pnl for o in outcomes]
    rng = random.Random(42)  # deterministic
    boots = []
    for _ in range(10000):
        sample = [pnls[rng.randrange(n)] for _ in range(n)]
        boots.append(sum(sample))
    boots.sort()
    lo = boots[int(0.025 * len(boots))]
    hi = boots[int(0.975 * len(boots))]
    print(
        f"- Total PnL bootstrap 95% CI (n=10000 resamples): [${lo:+.0f}, ${hi:+.0f}]"
    )
    annual_lo = lo * 365 / 30
    annual_hi = hi * 365 / 30
    print(f"- Annualized bootstrap 95% CI: [${annual_lo:+.0f}, ${annual_hi:+.0f}]/yr")


if __name__ == "__main__":
    main()
