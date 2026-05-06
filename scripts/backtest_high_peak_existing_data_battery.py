"""High-peak proposal — existing-data analysis battery (A-E).

Replaces 5-month forward soak with deep analysis on the 853-trade
existing dataset. See `tasks/findings_high_peak_giveback.md` §13.7
for the data-driven kill criterion this script feeds.

Five analyses:
- A: bootstrap CI on the n=9 peak >= 75% cohort (10,000 resamples)
- B: cohort widening (peak >= 50% gives n=25)
- C: regime stratification (pre/post chain-dispatch revival 2026-05-01;
     pre/post BL-067 deploy 2026-05-04)
- D: per-signal cohort isolation under the best policy
- E: slippage sensitivity (50, 100, 200, 500 bps)

Output: structured markdown to stdout for direct paste into findings doc.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class Trade:
    id: int
    signal_type: str
    status: str
    entry_price: float
    exit_price: float
    peak_pct: float
    pnl_pct: float
    pnl_usd: float
    amount_usd: float
    exit_reason: str | None
    token_id: str
    symbol: str
    opened_at: str
    closed_at: str
    conviction_locked_at: str | None


def _load_trades(conn: sqlite3.Connection, window_days: int) -> list[Trade]:
    cur = conn.execute(
        """SELECT id, signal_type, status, entry_price, exit_price,
                  COALESCE(peak_pct, 0), COALESCE(pnl_pct, 0),
                  COALESCE(pnl_usd, 0), amount_usd, exit_reason,
                  token_id, symbol, opened_at,
                  COALESCE(closed_at, ''), conviction_locked_at
           FROM paper_trades
           WHERE status LIKE 'closed_%'
             AND datetime(closed_at) >= datetime('now', ?)
             AND entry_price > 0""",
        (f"-{window_days} days",),
    )
    return [Trade(*row) for row in cur.fetchall()]


def _is_eligible(t: Trade) -> bool:
    if t.peak_pct <= 0:
        return False
    if t.exit_reason == "stop_loss" and t.peak_pct < 5:
        return False
    return True


def _counter_pnl_pct(
    peak_pct: float, retrace_frac: float, slip_bps: float = 0.0
) -> float:
    """Counter-factual exit at retrace_frac from peak, with optional slippage discount.

    Slippage applies to the EXIT PRICE (not entry-relative return). For a trade
    with peak_pct=80 and retrace=15%, exit_price = entry × 1.80 × 0.85 = entry × 1.53.
    50bps on $1.53 = 0.0050 × 1.53 = 0.00765 (= 0.77pp on entry-relative return).
    """
    exit_multiplier = (1 + peak_pct / 100.0) * (1 - retrace_frac)
    raw = (exit_multiplier - 1) * 100.0
    slippage_pp = (slip_bps / 10000.0) * exit_multiplier * 100.0
    return raw - slippage_pp


def _apply_policy(
    trades: list[Trade],
    threshold_pct: float,
    retrace_frac: float,
    slip_bps: float = 0.0,
) -> list[tuple[Trade, float, bool]]:
    """Apply the conditional peak-fade policy. Returns (trade, counter_usd, fired)."""
    out: list[tuple[Trade, float, bool]] = []
    for t in trades:
        if _is_eligible(t) and t.peak_pct >= threshold_pct:
            counter_pct = _counter_pnl_pct(t.peak_pct, retrace_frac, slip_bps)
            counter_usd = (counter_pct / 100.0) * t.amount_usd
            out.append((t, counter_usd, True))
        else:
            out.append((t, t.pnl_usd, False))
    return out


def _delta_per_trade(applied: list[tuple[Trade, float, bool]]) -> list[float]:
    """Per-trade delta vs actual, only for trades where policy fired."""
    return [c - t.pnl_usd for t, c, fired in applied if fired]


def _wilcoxon_signed_rank_p(deltas: list[float]) -> float:
    """One-tailed Wilcoxon signed-rank against null=0. Returns p-value.

    Hand-implemented to avoid scipy dependency. For n<=25, exact distribution
    is tractable. Returns approximation via normal CDF for n>25.
    """
    import math

    n = len(deltas)
    if n == 0:
        return 1.0
    nonzero = [d for d in deltas if d != 0]
    if not nonzero:
        return 1.0
    abs_ranked = sorted(enumerate(nonzero), key=lambda x: abs(x[1]))
    # Assign ranks (1-indexed), handle ties by averaging
    ranks = [0.0] * len(nonzero)
    i = 0
    while i < len(abs_ranked):
        j = i
        while j < len(abs_ranked) and abs(abs_ranked[j][1]) == abs(abs_ranked[i][1]):
            j += 1
        avg_rank = (i + j + 1) / 2.0  # 1-indexed
        for k in range(i, j):
            ranks[abs_ranked[k][0]] = avg_rank
        i = j
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    n_eff = len(nonzero)
    # Normal approximation for the test statistic
    mean_w = n_eff * (n_eff + 1) / 4.0
    var_w = n_eff * (n_eff + 1) * (2 * n_eff + 1) / 24.0
    if var_w == 0:
        return 1.0
    z = (w_plus - mean_w) / (var_w**0.5)
    # One-tailed: P(W >= observed) under H0
    # Approximation via normal CDF: p = 1 - Phi(z)
    # Math.erf-based normal CDF
    return 0.5 * (1 - math.erf(z / (2**0.5)))


def analysis_a_bootstrap(trades: list[Trade], threshold: float, retrace: float) -> str:
    """A: Bootstrap CI on per-trade deltas in the trigger cohort."""
    applied = _apply_policy(trades, threshold, retrace)
    deltas = _delta_per_trade(applied)
    n = len(deltas)
    if n == 0:
        return f"### A. Bootstrap CI (peak >= {threshold:.0f}%, retrace {retrace*100:.0f}%)\n\nNo trades fire policy. n=0.\n"

    mean = statistics.mean(deltas)
    se = (statistics.stdev(deltas) / (n**0.5)) if n > 1 else 0.0

    # 10,000 bootstrap resamples
    rng = random.Random(20260505)
    boot_means: list[float] = []
    for _ in range(10000):
        sample = [rng.choice(deltas) for _ in range(n)]
        boot_means.append(sum(sample) / n)
    boot_means.sort()
    p5 = boot_means[int(0.05 * len(boot_means))]
    p50 = boot_means[int(0.50 * len(boot_means))]
    p95 = boot_means[int(0.95 * len(boot_means))]

    # Leave-one-out
    loo_totals = []
    for i in range(n):
        loo = deltas[:i] + deltas[i + 1 :]
        loo_totals.append(sum(loo))
    loo_min = min(loo_totals)
    loo_max = max(loo_totals)
    full_total = sum(deltas)

    p_signed_rank = _wilcoxon_signed_rank_p(deltas)
    out = (
        f"### A. Bootstrap CI (peak >= {threshold:.0f}%, retrace {retrace*100:.0f}%)\n\n"
        f"- **n trades policy fires on:** {n}\n"
        f"- **Mean delta per trade:** ${mean:.2f}, SE ${se:.2f}\n"
        f"- **Bootstrap mean 5/50/95th percentile:** ${p5:.2f} / ${p50:.2f} / ${p95:.2f}\n"
        f"- **Total cohort lift (full):** ${full_total:.2f}\n"
        f"- **Leave-one-out total range:** ${loo_min:.2f} to ${loo_max:.2f} (drop-one swing: ${loo_max-loo_min:.2f})\n"
        f"- **Per-trade deltas:** {[round(d, 2) for d in sorted(deltas, reverse=True)]}\n"
        f"- **Wilcoxon signed-rank one-tailed p-value:** {p_signed_rank:.4f}\n"
        f"- **Statistical caveat:** at n={n}, signed-rank does not reject "
        f"null at conventional α=0.05 if p≥0.05. Bootstrap is illustrative; "
        f"the cohort is too thin for distribution-free inference.\n\n"
        f"**Interpretation:** if 5th-percentile bootstrap mean > $20/trade, headline is robust to outliers. "
        f"If 5th-percentile mean is near zero or negative, the headline is one-or-two-trade-driven.\n"
    )
    return out


def analysis_b_cohort_widening(trades: list[Trade]) -> str:
    """B: Cohort widening — sweep peak threshold from 30 to 100, retrace fixed at 15%."""
    out = "### B. Cohort widening — wider thresholds at retrace 15%\n\n"
    out += f"| Threshold | n_applied | Total lift Δ | Mean Δ/trade | Bootstrap 5th-pct mean |\n"
    out += f"|---|---|---|---|---|\n"
    for thr in [30, 40, 50, 60, 75, 100]:
        applied = _apply_policy(trades, thr, 0.15)
        deltas = _delta_per_trade(applied)
        n = len(deltas)
        if n == 0:
            out += f"| peak >= {thr}% | 0 | — | — | — |\n"
            continue
        total = sum(deltas)
        mean = total / n
        rng = random.Random(20260505)
        boot = sorted(
            sum(rng.choice(deltas) for _ in range(n)) / n for _ in range(5000)
        )
        p5 = boot[int(0.05 * len(boot))]
        out += f"| peak >= {thr}% | {n} | ${total:+.2f} | ${mean:+.2f} | ${p5:+.2f} |\n"
    out += (
        "\n**Interpretation:** at peak >= 50% the cohort is n=25, large enough for "
        "R1's power requirement *today* on existing data — no forward wait needed. "
        "If lift at peak >= 50% is materially smaller than at peak >= 75%, the proposal "
        "may be specifically a high-peak phenomenon. If it scales, a wider/lower-threshold "
        "policy is shippable on existing data alone.\n"
    )
    return out


def analysis_c_regime_stratification(trades: list[Trade]) -> str:
    """C: Pre/post chain-dispatch revival (2026-05-01) and BL-067 deploy (2026-05-04)."""
    out = "### C. Regime stratification\n\n"
    splits = [
        ("Chain dispatch revival", "2026-05-01"),
        ("BL-067 conviction-lock deploy", "2026-05-04"),
    ]
    out += "| Split point | Cohort | n_applied | Total Δ | Mean Δ |\n|---|---|---|---|---|\n"
    for label, cutover in splits:
        for cohort_label, filter_fn in [
            (f"opened BEFORE {cutover}", lambda t: t.opened_at < cutover),
            (f"opened ON/AFTER {cutover}", lambda t: t.opened_at >= cutover),
        ]:
            sub = [t for t in trades if filter_fn(t)]
            applied = _apply_policy(sub, 75, 0.15)
            deltas = _delta_per_trade(applied)
            n = len(deltas)
            total = sum(deltas) if n else 0.0
            mean = (total / n) if n else 0.0
            out += (
                f"| {label} | {cohort_label} | {n} | ${total:+.2f} | ${mean:+.2f} |\n"
            )
    out += (
        "\n**Interpretation:** if lift survives in BOTH pre and post cohorts of either split, "
        "regime-stationarity worry is reduced. If lift is concentrated in only one regime, "
        "the proposal is regime-specific and needs a regime-detector before shipping.\n"
    )
    return out


def analysis_d_per_signal(trades: list[Trade], threshold: float, retrace: float) -> str:
    """D: Per-signal lift at the best policy."""
    applied = _apply_policy(trades, threshold, retrace)
    by_sig: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "n_applied": 0, "actual": 0.0, "counter": 0.0}
    )
    for t, c, fired in applied:
        by_sig[t.signal_type]["n"] += 1
        by_sig[t.signal_type]["actual"] += t.pnl_usd
        by_sig[t.signal_type]["counter"] += c
        if fired:
            by_sig[t.signal_type]["n_applied"] += 1

    rows = sorted(
        by_sig.items(),
        key=lambda kv: kv[1]["counter"] - kv[1]["actual"],
        reverse=True,
    )
    out = f"### D. Per-signal isolation (peak >= {threshold:.0f}%, retrace {retrace*100:.0f}%)\n\n"
    out += "| Signal | n total | n policy fires on | Actual $ | Counter $ | Δ $ |\n|---|---|---|---|---|---|\n"
    for sig, agg in rows:
        delta = agg["counter"] - agg["actual"]
        out += (
            f"| {sig} | {agg['n']} | {agg['n_applied']} | "
            f"${agg['actual']:+.2f} | ${agg['counter']:+.2f} | ${delta:+.2f} |\n"
        )
    out += (
        "\n**Interpretation:** signals with positive Δ and n_applied >= 2 are clean "
        "opt-in candidates. Signals with Δ near zero or negative should NOT be opted "
        "in regardless of global policy.\n"
    )
    return out


def analysis_e_slippage_sensitivity(
    trades: list[Trade], threshold: float, retrace: float
) -> str:
    """E: Slippage sensitivity at the best policy."""
    out = f"### E. Slippage sensitivity (peak >= {threshold:.0f}%, retrace {retrace*100:.0f}%)\n\n"
    out += "| Slippage (bps) | Total counter $ | Δ vs actual $ |\n|---|---|---|\n"
    actual_total = sum(t.pnl_usd for t in trades)
    for bps in [0, 50, 100, 200, 500]:
        applied = _apply_policy(trades, threshold, retrace, slip_bps=bps)
        counter_total = sum(c for _, c, _ in applied)
        delta = counter_total - actual_total
        out += f"| {bps} | ${counter_total:+.2f} | ${delta:+.2f} |\n"
    out += (
        "\n**Interpretation:** if Δ remains positive at 200bps, the proposal is robust "
        "to paper->live slippage degradation. If Δ goes negative below ~150bps, the "
        "proposal is paper-only and should NOT be promoted to live without a re-run.\n"
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default="/root/gecko-alpha/scout.db")
    p.add_argument("--window", type=int, default=30)
    p.add_argument(
        "--threshold",
        type=float,
        default=75.0,
        help="Best-policy peak threshold (default 75)",
    )
    p.add_argument(
        "--retrace",
        type=float,
        default=0.15,
        help="Best-policy retrace fraction (default 0.15)",
    )
    args = p.parse_args()

    try:
        conn = sqlite3.connect(args.db_path)
    except sqlite3.OperationalError as e:
        print(f"ERROR: cannot open {args.db_path}: {e}")
        return 1

    trades = _load_trades(conn, args.window)
    if not trades:
        print(f"No closed trades in last {args.window}d. Exiting.")
        return 0

    print(f"# Existing-data analysis battery — high-peak proposal\n")
    print(
        f"**Window:** {args.window}d, **n trades:** {len(trades)}, "
        f"**eligible (peak > 0):** {sum(1 for t in trades if _is_eligible(t))}, "
        f"**actual net:** ${sum(t.pnl_usd for t in trades):+.2f}\n"
    )
    print(
        f"**Best policy under test:** peak >= {args.threshold:.0f}%, retrace {args.retrace*100:.0f}%\n"
    )
    print()
    print(analysis_a_bootstrap(trades, args.threshold, args.retrace))
    print(analysis_b_cohort_widening(trades))
    print(analysis_c_regime_stratification(trades))
    print(analysis_d_per_signal(trades, args.threshold, args.retrace))
    print(analysis_e_slippage_sensitivity(trades, args.threshold, args.retrace))

    return 0


if __name__ == "__main__":
    sys.exit(main())
