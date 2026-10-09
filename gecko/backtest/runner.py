from __future__ import annotations

import argparse
import sqlite3

from gecko.backtest.study import (
    RULES,
    apply_rule,
    battery,
    f4_divergence,
    load_study,
)
from gecko.jobs import register
from gecko.settings import GeckoSettings

HEADER = (
    "rule | n | mean | median | BTC | ex-best | ex-top3 | same-month R0 | "
    "%>=7x | %<=0.5x | halves | PASS | failed"
)


@register("backtest")
async def backtest_job(settings: GeckoSettings, ns: argparse.Namespace) -> int:
    if ns.rule not in RULES:
        print(f"backtest: unknown rule {ns.rule!r}; expected one of {RULES}")
        return 1
    stats: dict = {}
    try:
        coins = load_study(ns.study_dir, ns.history_db, stats)
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f"backtest: cannot load study: {exc}")
        return 1
    b = battery(apply_rule(coins, ns.rule, settings), coins)
    failed = ",".join(str(i + 1) for i, p in enumerate(b.passes) if not p)
    print(
        f"universe {stats['universe']} | live {len(coins)} | dead {stats['dead']} | "
        f"stored prices {stats['stored']} | excluded chart/stored >2x "
        f"{stats['excluded']}"
    )
    print(HEADER)
    print(
        f"{ns.rule} | {b.n} | {b.mean:.2f}x | {b.median:.2f}x | {b.btc_mean:.2f}x | "
        f"{b.ex_best:.2f}x | {b.ex_top3:.2f}x | {b.same_month_r0:.2f}x | "
        f"{b.pct_7x:.1f}% | {b.pct_half:.0f}% | {b.halves} | "
        f"{'PASS' if b.passed else 'fail'} | {failed}"
    )
    print(
        f"live-definition divergence: {f4_divergence(coins, settings)} coins "
        f"change F4 verdict"
    )
    return 0
