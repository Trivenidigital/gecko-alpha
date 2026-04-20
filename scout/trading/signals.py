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
    for spike in spikes:
        try:
            combo_key = build_combo_key(signal_type="volume_spike", signals=None)
            allow, reason = await should_open(db, combo_key, settings=settings)
            if not allow:
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
        except Exception:
            logger.exception(
                "trading_open_spike_error",
                coin_id=spike.get("coin_id"),
            )


async def trade_gainers(
    engine, db: Database, min_mcap: float = 5_000_000, *, settings
) -> None:
    """Open paper trades for newly detected top gainers.

    Filter: market_cap >= min_mcap to skip micro-cap junk.
    """
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
        if skipped_null_mcap or skipped_low_mcap:
            logger.info(
                "trade_gainers_filtered",
                total=len(new_gainers),
                skipped_null_mcap=skipped_null_mcap,
                skipped_low_mcap=skipped_low_mcap,
                min_mcap=min_mcap,
            )
        for g in new_gainers:
            if (g["market_cap"] or 0) < min_mcap:
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
    engine, db: Database, min_mcap: float = 5_000_000, *, settings
) -> None:
    """Open paper trades for newly detected top losers (contrarian play).

    Filter: market_cap >= min_mcap to skip micro-cap junk.
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
        if skipped_null_mcap or skipped_low_mcap:
            logger.info(
                "trade_losers_filtered",
                total=len(new_losers),
                skipped_null_mcap=skipped_null_mcap,
                skipped_low_mcap=skipped_low_mcap,
                min_mcap=min_mcap,
            )
        for l in new_losers:
            if (l["market_cap"] or 0) < min_mcap:
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
    *,
    settings,
) -> None:
    """Open paper trades on first meaningful signal for each token.

    This catches tokens at the EARLIEST detection point -- when they first
    show any scoring signal (quant > 0). This is the 'Early Catches' moment.

    The engine's duplicate check prevents re-opening for the same token.

    Args:
        scored_candidates: list of (CandidateToken, quant_score, signals_fired)
    """
    from datetime import datetime, timezone
    from scout.heartbeat import _heartbeat_stats
    from scout.trading.qualifier_state import classify_transitions

    # Filter to the qualifying set using the exact same predicate as before.
    qualifying: list[tuple] = []
    for token, quant_score, signals_fired in scored_candidates:
        if quant_score <= 0 or not signals_fired:
            continue
        if (token.market_cap_usd or 0) < min_mcap:
            continue
        if token.chain not in ("coingecko",):
            continue
        qualifying.append((token, quant_score, signals_fired))

    if not qualifying:
        return

    current_ids = {t.contract_address for t, _, _ in qualifying}
    now = datetime.now(timezone.utc)

    try:
        transitions = await classify_transitions(
            db,
            signal_type="first_signal",
            current_token_ids=current_ids,
            now=now,
            exit_grace_hours=settings.QUALIFIER_EXIT_GRACE_HOURS,
        )
    except Exception as exc:
        logger.error(
            "qualifier_classify_failed",
            err_id="QUALIFIER_CLASSIFY_FAIL",
            exc_type=type(exc).__name__,
            exc_info=True,
        )
        return  # fail-closed: skip all first_signal trades this cycle

    seen: set[str] = set()
    for token, quant_score, signals_fired in qualifying:
        if token.contract_address not in transitions:
            continue
        if token.contract_address in seen:
            continue  # multi-ingestor dedup
        seen.add(token.contract_address)

        prior_last = transitions[token.contract_address]  # str | None
        # Compute elapsed_since_prior_hours for observability. None on first-ever
        # qualification; parse ISO-8601 (fromisoformat handles UTC offsets).
        if prior_last is None:
            first_seen = True
            elapsed_hours: float | None = None
        else:
            first_seen = False
            try:
                prior_dt = datetime.fromisoformat(prior_last)
                elapsed_hours = (now - prior_dt).total_seconds() / 3600.0
            except ValueError:
                elapsed_hours = None

        logger.info(
            "qualifier_transition_fired",
            signal_type="first_signal",
            token_id=token.contract_address,
            first_seen=first_seen,
            prior_last_qualified_at=prior_last,
            elapsed_since_prior_hours=elapsed_hours,
        )
        _heartbeat_stats["qualifier_transitions"] += 1

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

            trade_id = await engine.open_trade(
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
            if trade_id is None:
                # NOTE (spec divergence): the spec's Observability section lists
                # possible reasons including `max_open_hit`, `cooldown`, etc.
                # engine.open_trade returns `int | None` without an accompanying
                # reason, so we cannot distinguish here. We log a single generic
                # reason `open_trade_returned_none`; downstream operators can
                # correlate with engine-side logs (warmup/dedup/cooldown/cap)
                # using `token_id` + timestamp. If finer-grained reasons are
                # required later, extend engine.open_trade to return (id, reason).
                logger.info(
                    "qualifier_transition_skipped",
                    signal_type="first_signal",
                    token_id=token.contract_address,
                    reason="open_trade_returned_none",
                )
                _heartbeat_stats["qualifier_skips"] += 1
        except Exception:
            logger.exception("trading_first_signal_error", token=token.ticker)
            _heartbeat_stats["qualifier_skips"] += 1


async def trade_trending(
    engine, db: Database, max_mcap_rank: int = 1500, *, settings
) -> None:
    """Open paper trades for newly trending tokens.

    Filter: market_cap_rank <= max_mcap_rank. CoinGecko rank is a rough
    liquidity proxy — rank 1500 corresponds to roughly the last legitimately
    tradable tokens; anything below tends to be illiquid micro-caps.
    Tokens without a rank (rank IS NULL) are skipped.
    """
    try:
        cursor = await db._conn.execute(
            """SELECT DISTINCT coin_id, symbol, name, market_cap_rank
               FROM trending_snapshots
               WHERE datetime(snapshot_at) >= datetime('now', '-5 minutes')
               AND coin_id NOT IN (
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
        if skipped_null_rank or skipped_low_rank:
            logger.info(
                "trade_trending_filtered",
                total=len(new_trending),
                skipped_null_rank=skipped_null_rank,
                skipped_low_rank=skipped_low_rank,
                max_mcap_rank=max_mcap_rank,
            )
        for t in new_trending:
            rank = t["market_cap_rank"]
            if rank is None or rank > max_mcap_rank:
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
    min_fit_score: int = 1,
    *,
    settings,
) -> None:
    """Open paper trades for narrative prediction picks.

    Filters:
    - mcap >= min_mcap (skip micro-cap junk)
    - narrative_fit_score > 0 (Claude must have actually scored it)
    - category not in junk blacklist (Zoo-Themed, Trading Bots, etc)
    """
    for pred in prediction_models:
        if pred.is_control:
            continue
        # Quality gate: skip micro-cap junk
        if pred.market_cap_at_prediction < min_mcap:
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
