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

logger = structlog.get_logger()


async def trade_volume_spikes(engine, db: Database, spikes: list[dict]) -> None:
    """Open paper trades for detected volume spikes."""
    for spike in spikes:
        try:
            await engine.open_trade(
                token_id=spike["coin_id"],
                chain="coingecko",
                signal_type="volume_spike",
                signal_data={"spike_ratio": spike.get("spike_ratio", 0)},
                entry_price=spike.get("current_price"),
            )
        except Exception:
            logger.exception(
                "trading_open_spike_error",
                coin_id=spike.get("coin_id"),
            )


async def trade_gainers(engine, db: Database) -> None:
    """Open paper trades for newly detected top gainers."""
    try:
        cursor = await db._conn.execute(
            """SELECT DISTINCT coin_id, symbol, name, price_change_24h, price_at_snapshot
               FROM gainers_snapshots
               WHERE snapshot_at >= datetime('now', '-5 minutes')
               AND coin_id NOT IN (
                   SELECT token_id FROM paper_trades WHERE signal_type = 'gainers_early' AND status = 'open'
               )"""
        )
        new_gainers = await cursor.fetchall()
        for g in new_gainers:
            try:
                await engine.open_trade(
                    token_id=g["coin_id"],
                    symbol=g["symbol"],
                    name=g["name"],
                    chain="coingecko",
                    signal_type="gainers_early",
                    signal_data={"price_change_24h": g["price_change_24h"]},
                    entry_price=g["price_at_snapshot"],
                )
            except Exception:
                logger.exception("trading_gainers_error", coin_id=g["coin_id"])
    except Exception:
        logger.exception("trading_gainers_query_error")


async def trade_losers(engine, db: Database) -> None:
    """Open paper trades for newly detected top losers (contrarian play)."""
    try:
        cursor = await db._conn.execute(
            """SELECT DISTINCT coin_id, symbol, name, price_change_24h, price_at_snapshot
               FROM losers_snapshots
               WHERE snapshot_at >= datetime('now', '-5 minutes')
               AND coin_id NOT IN (
                   SELECT token_id FROM paper_trades WHERE signal_type = 'losers_contrarian' AND status = 'open'
               )"""
        )
        new_losers = await cursor.fetchall()
        for l in new_losers:
            try:
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
                    signal_data={"price_change_24h": l["price_change_24h"]},
                    entry_price=loser_price,
                )
            except Exception:
                logger.exception("trading_losers_error", coin_id=l["coin_id"])
    except Exception:
        logger.exception("trading_losers_query_error")


async def trade_momentum(engine, momentum_7d: list[dict], min_mcap: float = 5_000_000) -> None:
    """Open paper trades for 7-day momentum tokens. Filters micro-cap junk."""
    for m in momentum_7d:
        if (m.get("market_cap") or 0) < min_mcap:
            continue
        try:
            await engine.open_trade(
                token_id=m["coin_id"],
                symbol=m["symbol"],
                name=m["name"],
                chain="coingecko",
                signal_type="momentum_7d",
                signal_data={
                    "change_7d": m["price_change_7d"],
                    "change_24h": m["price_change_24h"],
                    "mcap": m.get("market_cap"),
                },
                entry_price=m.get("current_price"),
            )
        except Exception:
            logger.exception(
                "trading_momentum_7d_error",
                coin_id=m.get("coin_id"),
            )


async def trade_trending(engine, db: Database) -> None:
    """Open paper trades for newly trending tokens."""
    try:
        cursor = await db._conn.execute(
            """SELECT DISTINCT coin_id, symbol, name FROM trending_snapshots
               WHERE snapshot_at >= datetime('now', '-5 minutes')
               AND coin_id NOT IN (
                   SELECT token_id FROM paper_trades WHERE signal_type = 'trending_catch' AND status = 'open'
               )"""
        )
        new_trending = await cursor.fetchall()
        for t in new_trending:
            pc = await db._conn.execute(
                "SELECT current_price FROM price_cache WHERE coin_id = ?",
                (t["coin_id"],),
            )
            price_row = await pc.fetchone()
            trending_price = price_row[0] if price_row else None
            await engine.open_trade(
                token_id=t["coin_id"],
                symbol=t["symbol"],
                name=t["name"],
                chain="coingecko",
                signal_type="trending_catch",
                signal_data={"source": "trending_snapshot"},
                entry_price=trending_price,
            )
    except Exception:
        logger.exception("trading_trending_catch_error")


async def trade_predictions(
    engine, db: Database, prediction_models: list,
    min_mcap: float = 5_000_000,
    min_fit_score: int = 0,
) -> None:
    """Open paper trades for narrative prediction picks.

    Filters: only trade tokens with mcap >= min_mcap to avoid micro-cap junk
    from niche categories (Zoo-Themed, Trading Bots, Arcade Games, etc).
    """
    for pred in prediction_models:
        if pred.is_control:
            continue
        # Quality gate: skip micro-cap junk
        if pred.market_cap_at_prediction < min_mcap:
            continue
        # Quality gate: skip low-confidence picks
        if min_fit_score > 0 and (pred.narrative_fit_score or 0) < min_fit_score:
            continue
        try:
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
            )
            except Exception:
                logger.exception(
                    "trading_open_narrative_error",
                    coin_id=pred.coin_id,
                )


async def trade_chain_completions(engine, db: Database, settings) -> None:
    """Open paper trades for completed chain pattern matches."""
    try:
        cursor = await db._conn.execute(
            """SELECT DISTINCT token_id, pattern_id, pattern_name, conviction_boost, pipeline
               FROM chain_matches
               WHERE completed_at >= datetime('now', '-5 minutes')
               AND token_id NOT IN (
                   SELECT token_id FROM paper_trades WHERE signal_type = 'chain_completed' AND status = 'open'
               )"""
        )
        new_chains = await cursor.fetchall()
        for c in new_chains:
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
            )
    except Exception:
        logger.exception("trading_chain_complete_error")
