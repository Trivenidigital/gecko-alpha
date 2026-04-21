"""Trading signal dispatch helpers.

Each function wraps a query + for-loop + try/except + open_trade pattern
that was previously inlined in main.py and narrative/agent.py.

Note: These functions access db._conn directly for read queries.
This is consistent with the rest of the codebase (evaluator, tracker,
observer all use the same pattern). Adding public Database methods for
one-off signal queries would add complexity without benefit.
"""

import structlog

from scout.db import Database
from scout.trading.combo_key import build_combo_key
from scout.trading.suppression import should_open

logger = structlog.get_logger()


async def trade_volume_spikes(
    engine, db: Database, spikes: list[dict], settings
) -> None:
    """Open paper trades for detected volume spikes."""
    skipped_suppressed = 0
    errors = 0
    opened = 0
    for spike in spikes:
        try:
            combo_key = build_combo_key(signal_type="volume_spike", signals=None)
            allow, reason = await should_open(db, combo_key, settings=settings)
            if not allow:
                skipped_suppressed += 1
                logger.info(
                    "signal_suppressed",
                    combo_key=combo_key,
                    reason=reason,
                    coin_id=spike.get("coin_id"),
                    signal_type="volume_spike",
                )
                continue
            await engine.open_trade(
                token_id=spike["coin_id"],
                chain="coingecko",
                signal_type="volume_spike",
                signal_data={"spike_ratio": spike.get("spike_ratio", 0)},
                entry_price=spike.get("current_price"),
                signal_combo=combo_key,
            )
            opened += 1
        except Exception:
            errors += 1
            logger.exception(
                "trading_open_spike_error",
                coin_id=spike.get("coin_id"),
            )
    logger.info(
        "trade_volume_spikes_filtered",
        total=len(spikes),
        skipped_suppressed=skipped_suppressed,
        errors=errors,
        opened=opened,
    )


async def trade_gainers(
    engine,
    db: Database,
    min_mcap: float = 5_000_000,
    max_mcap: float | None = None,
    *,
    settings,
) -> None:
    """Open paper trades for newly detected top gainers.

    Filter: market_cap in [min_mcap, max_mcap] to skip micro-cap junk and
    majors that rarely pump fast enough to hit PAPER_TP_PCT within the
    holding window. Signals/alerts for large caps still fire elsewhere —
    only paper-trade admission is gated.
    Late-pump filter: when PAPER_GAINERS_MAX_24H_PCT > 0, skip tokens whose
    24h change exceeds the threshold. Set the knob to 0 to disable the filter.
    """
    max_24h = settings.PAPER_GAINERS_MAX_24H_PCT
    try:
        cursor = await db._conn.execute(
            """SELECT DISTINCT coin_id, symbol, name, price_change_24h,
                               price_at_snapshot, market_cap
               FROM gainers_snapshots
               WHERE datetime(snapshot_at) >= datetime('now', '-5 minutes')
               AND coin_id NOT IN (
                   SELECT token_id FROM paper_trades WHERE signal_type = 'gainers_early' AND status = 'open'
               )"""
        )
        new_gainers = await cursor.fetchall()
        skipped_null_mcap = sum(1 for g in new_gainers if g["market_cap"] is None)
        skipped_low_mcap = sum(
            1
            for g in new_gainers
            if g["market_cap"] is not None and g["market_cap"] < min_mcap
        )
        skipped_large_mcap = sum(
            1
            for g in new_gainers
            if max_mcap is not None
            and g["market_cap"] is not None
            and g["market_cap"] > max_mcap
        )
        skipped_late_pump = sum(
            1
            for g in new_gainers
            if max_24h > 0
            and g["price_change_24h"] is not None
            and g["price_change_24h"] > max_24h
        )
        logger.info(
            "trade_gainers_filtered",
            total=len(new_gainers),
            skipped_null_mcap=skipped_null_mcap,
            skipped_low_mcap=skipped_low_mcap,
            skipped_large_mcap=skipped_large_mcap,
            skipped_late_pump=skipped_late_pump,
            min_mcap=min_mcap,
            max_mcap=max_mcap,
            max_24h_pct=max_24h,
        )
        for g in new_gainers:
            if (g["market_cap"] or 0) < min_mcap:
                continue
            if max_mcap is not None and (g["market_cap"] or 0) > max_mcap:
                continue
            change_24h = g["price_change_24h"]
            if max_24h > 0:
                # Reject rows with no 24h change when the late-pump filter is
                # active. gainers_snapshots.price_change_24h is NOT NULL in the
                # current schema; this guard is cheap defense-in-depth against a
                # future schema change that nullifies the column.
                if change_24h is None or change_24h > max_24h:
                    continue
            try:
                combo_key = build_combo_key(signal_type="gainers_early", signals=None)
                allow, reason = await should_open(db, combo_key, settings=settings)
                if not allow:
                    logger.info(
                        "signal_suppressed",
                        combo_key=combo_key,
                        reason=reason,
                        coin_id=g["coin_id"],
                        signal_type="gainers_early",
                    )
                    continue
                await engine.open_trade(
                    token_id=g["coin_id"],
                    symbol=g["symbol"],
                    name=g["name"],
                    chain="coingecko",
                    signal_type="gainers_early",
                    signal_data={
                        "price_change_24h": g["price_change_24h"],
                        "mcap": g["market_cap"],
                    },
                    entry_price=g["price_at_snapshot"],
                    signal_combo=combo_key,
                )
            except Exception:
                logger.exception("trading_gainers_error", coin_id=g["coin_id"])
    except Exception:
        logger.exception("trading_gainers_query_error")


async def trade_losers(
    engine,
    db: Database,
    min_mcap: float = 5_000_000,
    max_mcap: float | None = None,
    *,
    settings,
) -> None:
    """Open paper trades for newly detected top losers (contrarian play).

    Filter: market_cap in [min_mcap, max_mcap] to skip micro-cap junk and
    majors that rarely snap back fast enough for a contrarian paper trade.
    Signals/alerts for large caps still fire elsewhere — only paper-trade
    admission is gated.
    """
    try:
        cursor = await db._conn.execute(
            """SELECT DISTINCT coin_id, symbol, name, price_change_24h,
                               price_at_snapshot, market_cap
               FROM losers_snapshots
               WHERE datetime(snapshot_at) >= datetime('now', '-5 minutes')
               AND coin_id NOT IN (
                   SELECT token_id FROM paper_trades WHERE signal_type = 'losers_contrarian' AND status = 'open'
               )"""
        )
        new_losers = await cursor.fetchall()
        skipped_null_mcap = sum(1 for l in new_losers if l["market_cap"] is None)
        skipped_low_mcap = sum(
            1
            for l in new_losers
            if l["market_cap"] is not None and l["market_cap"] < min_mcap
        )
        skipped_large_mcap = sum(
            1
            for l in new_losers
            if max_mcap is not None
            and l["market_cap"] is not None
            and l["market_cap"] > max_mcap
        )
        logger.info(
            "trade_losers_filtered",
            total=len(new_losers),
            skipped_null_mcap=skipped_null_mcap,
            skipped_low_mcap=skipped_low_mcap,
            skipped_large_mcap=skipped_large_mcap,
            min_mcap=min_mcap,
            max_mcap=max_mcap,
        )
        for l in new_losers:
            if (l["market_cap"] or 0) < min_mcap:
                continue
            if max_mcap is not None and (l["market_cap"] or 0) > max_mcap:
                continue
            try:
                combo_key = build_combo_key(
                    signal_type="losers_contrarian", signals=None
                )
                allow, reason = await should_open(db, combo_key, settings=settings)
                if not allow:
                    logger.info(
                        "signal_suppressed",
                        combo_key=combo_key,
                        reason=reason,
                        coin_id=l["coin_id"],
                        signal_type="losers_contrarian",
                    )
                    continue
                loser_price = l["price_at_snapshot"]
                if not loser_price:
                    pc = await db._conn.execute(
                        "SELECT current_price FROM price_cache WHERE coin_id = ?",
                        (l["coin_id"],),
                    )
                    price_row = await pc.fetchone()
                    loser_price = price_row[0] if price_row else None
                await engine.open_trade(
                    token_id=l["coin_id"],
                    symbol=l["symbol"],
                    name=l["name"],
                    chain="coingecko",
                    signal_type="losers_contrarian",
                    signal_data={
                        "price_change_24h": l["price_change_24h"],
                        "mcap": l["market_cap"],
                    },
                    entry_price=loser_price,
                    signal_combo=combo_key,
                )
            except Exception:
                logger.exception("trading_losers_error", coin_id=l["coin_id"])
    except Exception:
        logger.exception("trading_losers_query_error")


async def trade_first_signals(
    engine,
    db: Database,
    scored_candidates: list,
    min_mcap: float = 5_000_000,
    max_mcap: float | None = None,
    *,
    settings,
) -> None:
    """Open paper trades on first meaningful signal for each token.

    This catches tokens at the EARLIEST detection point -- when they first
    show any scoring signal (quant > 0). This is the 'Early Catches' moment.

    The engine's duplicate check prevents re-opening for the same token.

    Args:
        scored_candidates: list of (CandidateToken, quant_score, signals_fired)
        max_mcap: optional upper mcap cap — tokens with market_cap_usd above
            this are skipped from paper-trade admission; signals/alerts are
            unaffected.
    """
    skipped_large = 0
    for token, quant_score, signals_fired in scored_candidates:
        if quant_score <= 0 or not signals_fired:
            continue
        if (token.market_cap_usd or 0) < min_mcap:
            continue
        if max_mcap is not None and (token.market_cap_usd or 0) > max_mcap:
            skipped_large += 1
            continue
        # Focus on CoinGecko-listed tokens, skip DEX memecoins
        if token.chain not in ("coingecko",):
            continue
        try:
            sigs = signals_fired
            combo_key = build_combo_key(signal_type="first_signal", signals=sigs)
            allow, reason = await should_open(db, combo_key, settings=settings)
            if not allow:
                logger.info(
                    "signal_suppressed",
                    combo_key=combo_key,
                    reason=reason,
                    coin_id=token.contract_address,
                    signal_type="first_signal",
                )
                continue
            pc = await db._conn.execute(
                "SELECT current_price FROM price_cache WHERE coin_id = ?",
                (token.contract_address,),
            )
            pr = await pc.fetchone()
            price = pr[0] if pr else None

            await engine.open_trade(
                token_id=token.contract_address,
                symbol=token.ticker,
                name=token.token_name,
                chain=token.chain,
                signal_type="first_signal",
                signal_data={
                    "quant_score": quant_score,
                    "signals": signals_fired,
                },
                entry_price=price,
                signal_combo=combo_key,
            )
        except Exception:
            logger.exception("trading_first_signal_error", token=token.ticker)
    if skipped_large:
        logger.info(
            "trade_first_signals_filtered",
            skipped_large_mcap=skipped_large,
            max_mcap=max_mcap,
        )


async def trade_trending(
    engine,
    db: Database,
    max_mcap_rank: int = 1500,
    min_mcap: float = 5_000_000,
    max_mcap: float | None = None,
    *,
    settings,
) -> None:
    """Open paper trades for newly trending tokens.

    Two orthogonal gates:
    - market_cap in [min_mcap, max_mcap] — same floor/ceiling the other
      paper-trade signals use. Large caps rarely pump fast enough to hit
      PAPER_TP_PCT; micro-caps are junk. Signals/alerts still fire for
      out-of-range tokens — only paper-trade admission is gated.
    - market_cap_rank <= max_mcap_rank — illiquidity defense-in-depth.
      Tokens below rank ~1500 are typically too thin to trade reliably even
      when mcap looks fine (e.g. from unlock schedules).

    mcap is read from price_cache (trending_snapshots stores only rank),
    LEFT JOIN so tokens with no price row surface as NULL mcap and are
    skipped — we shouldn't open a trade without a fresh reference price.
    """
    try:
        cursor = await db._conn.execute(
            """SELECT DISTINCT ts.coin_id, ts.symbol, ts.name, ts.market_cap_rank,
                               pc.current_price, pc.market_cap
               FROM trending_snapshots ts
               LEFT JOIN price_cache pc ON pc.coin_id = ts.coin_id
               WHERE datetime(ts.snapshot_at) >= datetime('now', '-5 minutes')
               AND ts.coin_id NOT IN (
                   SELECT token_id FROM paper_trades WHERE signal_type = 'trending_catch' AND status = 'open'
               )"""
        )
        new_trending = await cursor.fetchall()
        skipped_null_rank = sum(1 for t in new_trending if t["market_cap_rank"] is None)
        skipped_low_rank = sum(
            1
            for t in new_trending
            if t["market_cap_rank"] is not None and t["market_cap_rank"] > max_mcap_rank
        )
        skipped_null_mcap = sum(1 for t in new_trending if t["market_cap"] is None)
        skipped_low_mcap = sum(
            1
            for t in new_trending
            if t["market_cap"] is not None and t["market_cap"] < min_mcap
        )
        skipped_large_mcap = sum(
            1
            for t in new_trending
            if max_mcap is not None
            and t["market_cap"] is not None
            and t["market_cap"] > max_mcap
        )
        if (
            skipped_null_rank
            or skipped_low_rank
            or skipped_null_mcap
            or skipped_low_mcap
            or skipped_large_mcap
        ):
            logger.info(
                "trade_trending_filtered",
                total=len(new_trending),
                skipped_null_rank=skipped_null_rank,
                skipped_low_rank=skipped_low_rank,
                skipped_null_mcap=skipped_null_mcap,
                skipped_low_mcap=skipped_low_mcap,
                skipped_large_mcap=skipped_large_mcap,
                max_mcap_rank=max_mcap_rank,
                min_mcap=min_mcap,
                max_mcap=max_mcap,
            )
        for t in new_trending:
            rank = t["market_cap_rank"]
            if rank is None or rank > max_mcap_rank:
                continue
            mcap = t["market_cap"]
            if mcap is None or mcap < min_mcap:
                continue
            if max_mcap is not None and mcap > max_mcap:
                continue
            try:
                combo_key = build_combo_key(signal_type="trending_catch", signals=None)
                allow, reason = await should_open(db, combo_key, settings=settings)
                if not allow:
                    logger.info(
                        "signal_suppressed",
                        combo_key=combo_key,
                        reason=reason,
                        coin_id=t["coin_id"],
                        signal_type="trending_catch",
                    )
                    continue
                trending_price = t["current_price"]
                await engine.open_trade(
                    token_id=t["coin_id"],
                    symbol=t["symbol"],
                    name=t["name"],
                    chain="coingecko",
                    signal_type="trending_catch",
                    signal_data={
                        "source": "trending_snapshot",
                        "mcap_rank": rank,
                    },
                    entry_price=trending_price,
                    signal_combo=combo_key,
                )
            except Exception:
                logger.exception("trading_trending_error", coin_id=t["coin_id"])
    except Exception:
        logger.exception("trading_trending_catch_error")


_JUNK_CATEGORIES = {
    "zoo-themed",
    "trading bots",
    "arcade games",
    "runes",
    "bridged stablecoin",
    "bridged tokens",
    "stablecoins",
    "wrapped tokens",
    "lp tokens",
    "memorial themed",
    "sticker-themed coins",
    "gotchiverse",
    "drc-20",
    "four.meme ecosystem (bnb memes)",
    "bonk.fun ecosystem",
    "pump.fun creator",
    "pump fund portfolio",
    "meme-token",
    "dog-themed",
    "cat-themed",
    "frog-themed",
    "solana-meme-coins",
    "base-meme-coins",
    "pump.fun ecosystem",
    "bnb-meme-coins",
    "ethereum-meme-coins",
    "trx-meme-coins",
    "avax-meme-coins",
    "fan-tokens",
}


async def trade_predictions(
    engine,
    db: Database,
    prediction_models: list,
    min_mcap: float = 5_000_000,
    max_mcap: float | None = None,
    min_fit_score: int = 1,
    *,
    settings,
) -> None:
    """Open paper trades for narrative prediction picks.

    Filters:
    - mcap in [min_mcap, max_mcap] (skip micro-cap junk + majors that rarely
      pump fast enough to hit PAPER_TP_PCT within the holding window;
      signals/alerts still fire for large caps)
    - narrative_fit_score > 0 (Claude must have actually scored it)
    - category not in junk blacklist (Zoo-Themed, Trading Bots, etc)
    """
    skipped_control = sum(1 for p in prediction_models if p.is_control)
    skipped_low_mcap = sum(
        1
        for p in prediction_models
        if not p.is_control and p.market_cap_at_prediction < min_mcap
    )
    skipped_large_mcap = sum(
        1
        for p in prediction_models
        if not p.is_control
        and max_mcap is not None
        and p.market_cap_at_prediction > max_mcap
    )
    skipped_low_fit = sum(
        1
        for p in prediction_models
        if not p.is_control
        and p.market_cap_at_prediction >= min_mcap
        and (max_mcap is None or p.market_cap_at_prediction <= max_mcap)
        and (p.narrative_fit_score or 0) < min_fit_score
    )
    skipped_junk = sum(
        1
        for p in prediction_models
        if not p.is_control
        and p.market_cap_at_prediction >= min_mcap
        and (max_mcap is None or p.market_cap_at_prediction <= max_mcap)
        and (p.narrative_fit_score or 0) >= min_fit_score
        and p.category_name
        and p.category_name.lower().strip() in _JUNK_CATEGORIES
    )
    logger.info(
        "trade_predictions_filtered",
        total=len(prediction_models),
        skipped_control=skipped_control,
        skipped_low_mcap=skipped_low_mcap,
        skipped_large_mcap=skipped_large_mcap,
        skipped_low_fit=skipped_low_fit,
        skipped_junk=skipped_junk,
        min_mcap=min_mcap,
        max_mcap=max_mcap,
        min_fit_score=min_fit_score,
    )
    for pred in prediction_models:
        if pred.is_control:
            continue
        # Quality gate: skip micro-cap junk
        if pred.market_cap_at_prediction < min_mcap:
            continue
        # Quality gate: skip majors (consume slots without producing wins)
        if max_mcap is not None and pred.market_cap_at_prediction > max_mcap:
            continue
        # Quality gate: Claude must have scored it (fit > 0)
        if (pred.narrative_fit_score or 0) < min_fit_score:
            continue
        # Quality gate: skip junk categories
        if (
            pred.category_name
            and pred.category_name.lower().strip() in _JUNK_CATEGORIES
        ):
            continue
        try:
            combo_key = build_combo_key(
                signal_type="narrative_prediction", signals=None
            )
            allow, reason = await should_open(db, combo_key, settings=settings)
            if not allow:
                logger.info(
                    "signal_suppressed",
                    combo_key=combo_key,
                    reason=reason,
                    coin_id=pred.coin_id,
                    signal_type="narrative_prediction",
                )
                continue
            pc = await db._conn.execute(
                "SELECT current_price FROM price_cache WHERE coin_id = ?",
                (pred.coin_id,),
            )
            pr = await pc.fetchone()
            pred_price = pr[0] if pr else None
            await engine.open_trade(
                token_id=pred.coin_id,
                chain="coingecko",
                signal_type="narrative_prediction",
                signal_data={
                    "fit": pred.narrative_fit_score,
                    "category": pred.category_name,
                    "mcap": pred.market_cap_at_prediction,
                },
                entry_price=pred_price,
                signal_combo=combo_key,
            )
        except Exception:
            logger.exception(
                "trading_open_narrative_error",
                coin_id=pred.coin_id,
            )


async def trade_chain_completions(engine, db: Database, *, settings) -> None:
    """Open paper trades for completed chain pattern matches."""
    try:
        cursor = await db._conn.execute(
            """SELECT DISTINCT token_id, pattern_id, pattern_name, conviction_boost, pipeline
               FROM chain_matches
               WHERE datetime(completed_at) >= datetime('now', '-5 minutes')
               AND token_id NOT IN (
                   SELECT token_id FROM paper_trades WHERE signal_type = 'chain_completed' AND status = 'open'
               )"""
        )
        new_chains = await cursor.fetchall()
        for c in new_chains:
            try:
                combo_key = build_combo_key(signal_type="chain_completed", signals=None)
                allow, reason = await should_open(db, combo_key, settings=settings)
                if not allow:
                    logger.info(
                        "signal_suppressed",
                        combo_key=combo_key,
                        reason=reason,
                        coin_id=c["token_id"],
                        signal_type="chain_completed",
                    )
                    continue
                pc = await db._conn.execute(
                    "SELECT current_price FROM price_cache WHERE coin_id = ?",
                    (c["token_id"],),
                )
                price_row = await pc.fetchone()
                chain_price = price_row[0] if price_row else None
                await engine.open_trade(
                    token_id=c["token_id"],
                    chain="coingecko",
                    signal_type="chain_completed",
                    signal_data={
                        "pattern": c["pattern_name"],
                        "boost": c["conviction_boost"],
                    },
                    entry_price=chain_price,
                    signal_combo=combo_key,
                )
            except Exception:
                logger.exception("trading_chain_error", token_id=c["token_id"])
    except Exception:
        logger.exception("trading_chain_complete_error")
