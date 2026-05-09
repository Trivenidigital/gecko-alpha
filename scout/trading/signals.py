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
from scout.spikes.models import VolumeSpike
from scout.trading.combo_key import build_combo_key
from scout.trading.suppression import should_open

logger = structlog.get_logger()


async def trade_volume_spikes(
    engine, db: Database, spikes: list[VolumeSpike], settings
) -> None:
    """Open paper trades for detected volume spikes."""
    skipped_suppressed = 0
    skipped_junk = 0
    errors = 0
    opened = 0
    for spike in spikes:
        if not _is_tradeable_candidate(spike.coin_id, spike.symbol):
            skipped_junk += 1
            logger.warning(
                "signal_skipped_junk",
                coin_id=spike.coin_id,
                symbol=spike.symbol,
                signal_type="volume_spike",
            )
            continue
        try:
            combo_key = build_combo_key(signal_type="volume_spike", signals=None)
            allow, reason = await should_open(db, combo_key, settings=settings)
            if not allow:
                skipped_suppressed += 1
                logger.info(
                    "signal_suppressed",
                    combo_key=combo_key,
                    reason=reason,
                    coin_id=spike.coin_id,
                    signal_type="volume_spike",
                )
                continue
            await engine.open_trade(
                token_id=spike.coin_id,
                symbol=spike.symbol,  # BL-076 Bug 2 fix
                name=spike.name,  # BL-076 Bug 2 fix
                chain="coingecko",
                signal_type="volume_spike",
                signal_data={"spike_ratio": spike.spike_ratio},
                entry_price=spike.price,
                signal_combo=combo_key,
            )
            opened += 1
        except Exception:
            errors += 1
            logger.exception(
                "trading_open_spike_error",
                coin_id=spike.coin_id,
            )
    logger.info(
        "trade_volume_spikes_filtered",
        total=len(spikes),
        skipped_suppressed=skipped_suppressed,
        skipped_junk=skipped_junk,
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
        skipped_junk = 0
        for g in new_gainers:
            if not _is_tradeable_candidate(g["coin_id"], g["symbol"]):
                skipped_junk += 1
                logger.warning(
                    "signal_skipped_junk",
                    coin_id=g["coin_id"],
                    symbol=g["symbol"],
                    signal_type="gainers_early",
                )
                continue
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
        logger.info(
            "trade_gainers_filtered",
            total=len(new_gainers),
            skipped_null_mcap=skipped_null_mcap,
            skipped_low_mcap=skipped_low_mcap,
            skipped_large_mcap=skipped_large_mcap,
            skipped_late_pump=skipped_late_pump,
            skipped_junk=skipped_junk,
            min_mcap=min_mcap,
            max_mcap=max_mcap,
            max_24h_pct=max_24h,
        )
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
        skipped_junk = 0
        for l in new_losers:
            if not _is_tradeable_candidate(l["coin_id"], l["symbol"]):
                skipped_junk += 1
                logger.warning(
                    "signal_skipped_junk",
                    coin_id=l["coin_id"],
                    symbol=l["symbol"],
                    signal_type="losers_contrarian",
                )
                continue
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
        logger.info(
            "trade_losers_filtered",
            total=len(new_losers),
            skipped_null_mcap=skipped_null_mcap,
            skipped_low_mcap=skipped_low_mcap,
            skipped_large_mcap=skipped_large_mcap,
            skipped_junk=skipped_junk,
            min_mcap=min_mcap,
            max_mcap=max_mcap,
        )
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
    skipped_junk = 0
    for token, quant_score, signals_fired in scored_candidates:
        if quant_score <= 0 or not signals_fired:
            continue
        if len(signals_fired) < settings.FIRST_SIGNAL_MIN_SIGNAL_COUNT:
            continue
        if not _is_tradeable_candidate(token.contract_address, token.ticker):
            skipped_junk += 1
            logger.warning(
                "signal_skipped_junk",
                coin_id=token.contract_address,
                symbol=token.ticker,
                signal_type="first_signal",
            )
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
    if skipped_large or skipped_junk:
        logger.info(
            "trade_first_signals_filtered",
            skipped_large_mcap=skipped_large,
            skipped_junk=skipped_junk,
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
        skipped_junk = 0
        for t in new_trending:
            if not _is_tradeable_candidate(t["coin_id"], t["symbol"]):
                skipped_junk += 1
                logger.warning(
                    "signal_skipped_junk",
                    coin_id=t["coin_id"],
                    symbol=t["symbol"],
                    signal_type="trending_catch",
                )
                continue
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
        if (
            skipped_null_rank
            or skipped_low_rank
            or skipped_null_mcap
            or skipped_low_mcap
            or skipped_large_mcap
            or skipped_junk
        ):
            logger.info(
                "trade_trending_filtered",
                total=len(new_trending),
                skipped_null_rank=skipped_null_rank,
                skipped_low_rank=skipped_low_rank,
                skipped_null_mcap=skipped_null_mcap,
                skipped_low_mcap=skipped_low_mcap,
                skipped_large_mcap=skipped_large_mcap,
                skipped_junk=skipped_junk,
                max_mcap_rank=max_mcap_rank,
                min_mcap=min_mcap,
                max_mcap=max_mcap,
            )
    except Exception:
        logger.exception("trading_trending_catch_error")


_JUNK_CATEGORIES = {
    "zoo themed",
    "trading bots",
    "arcade games",
    "runes",
    "bridged stablecoin",
    "bridged tokens",
    "stablecoins",
    "wrapped tokens",
    "lp tokens",
    "memorial themed",
    "sticker themed coins",
    "gotchiverse",
    "drc 20",
    "four.meme ecosystem (bnb memes)",
    "bonk.fun ecosystem",
    "pump.fun creator",
    "pump fund portfolio",
    "meme token",
    "dog themed",
    "cat themed",
    "frog themed",
    "solana meme coins",
    "base meme coins",
    "pump.fun ecosystem",
    "bnb meme coins",
    "ethereum meme coins",
    "trx meme coins",
    "avax meme coins",
    "fan tokens",
    "stock market themed",
    "metadao launchpad",
    "desci meme",
    "music",
    "airdropped tokens by nft projects",
    "trading card rwa platform",
    "murad picks",
}


def _normalize_category(name: str) -> str:
    """Lowercase + collapse hyphens/underscores to spaces for blacklist match.

    CoinGecko returns the same category as 'Bridged-Tokens' on some endpoints
    and 'Bridged Tokens' on others; this normalizes both to 'bridged tokens'."""
    return name.lower().strip().replace("-", " ").replace("_", " ")


# Junk-coin filter helpers extracted to scout/trading/filters.py to allow
# scout/narrative/predictor.py to apply _is_tradeable_candidate upstream
# (defense in depth) without forming a predictor → signals → predictor
# circular import. Re-exported here for back-compat with existing callers.
from scout.trading.filters import (  # noqa: F401  re-exports for back-compat
    _is_junk_coinid,
    _is_tradeable_candidate,
    _JUNK_COINID_PREFIXES,
    _JUNK_COINID_SUBSTRINGS,
)


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
    skipped_junk_category = sum(
        1
        for p in prediction_models
        if not p.is_control
        and p.market_cap_at_prediction >= min_mcap
        and (max_mcap is None or p.market_cap_at_prediction <= max_mcap)
        and (p.narrative_fit_score or 0) >= min_fit_score
        and p.category_name
        and _normalize_category(p.category_name) in _JUNK_CATEGORIES
    )
    skipped_junk_coinid = sum(
        1
        for p in prediction_models
        if not p.is_control
        and p.market_cap_at_prediction >= min_mcap
        and (max_mcap is None or p.market_cap_at_prediction <= max_mcap)
        and (p.narrative_fit_score or 0) >= min_fit_score
        and not (
            p.category_name and _normalize_category(p.category_name) in _JUNK_CATEGORIES
        )
        and _is_junk_coinid(p.coin_id)
    )
    logger.info(
        "trade_predictions_filtered",
        total=len(prediction_models),
        skipped_control=skipped_control,
        skipped_low_mcap=skipped_low_mcap,
        skipped_large_mcap=skipped_large_mcap,
        skipped_low_fit=skipped_low_fit,
        skipped_junk_category=skipped_junk_category,
        skipped_junk_coinid=skipped_junk_coinid,
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
            and _normalize_category(pred.category_name) in _JUNK_CATEGORIES
        ):
            continue
        # Quality gate: skip wrapped/bridged coin_id patterns regardless of category
        if _is_junk_coinid(pred.coin_id):
            continue
        # narrative_prediction token_id existence gate (this PR — adv-M1/M2,
        # arch-A2). Position is load-bearing: AFTER _is_junk_coinid (so the
        # gate doesn't double-fire on junk-prefix IDs) and BEFORE should_open
        # (so the skip event is always visible regardless of combo
        # suppression state). Fail-CLOSED on infra exception per project
        # convention — operator-aggregator dashboards filter by
        # signal_skipped_synthetic_token_id.
        if not pred.coin_id or not pred.coin_id.strip():
            logger.info(
                "signal_skipped_synthetic_token_id",
                coin_id=pred.coin_id,
                symbol=pred.symbol,
                signal_type="narrative_prediction",
                reason="empty_or_whitespace_coin_id",
            )
            continue
        # PR #72 H1: narrowed exception handling. asyncio.CancelledError
        # MUST propagate (don't swallow shutdown); AttributeError MUST
        # propagate (helper signature drift). Only the documented raises
        # (DbNotInitializedError + CoinIdResolutionError) trigger the
        # fail-CLOSED reject path. PR #72 H2: distinct reason field per
        # exception class for catastrophic-vs-transient paging.
        from scout.db import (
            CoinIdResolutionError,
            DbNotInitializedError,
        )

        try:
            resolves = await db.coin_id_resolves(pred.coin_id)
        except DbNotInitializedError as exc:
            logger.warning(
                "signal_skipped_synthetic_token_id",
                coin_id=pred.coin_id,
                symbol=pred.symbol,
                signal_type="narrative_prediction",
                reason="db_not_initialized",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue
        except CoinIdResolutionError as exc:
            # PR #72 M1: fail-CLOSED ≠ info noise; warning-level so
            # operator dashboards aggregate it for paging.
            logger.warning(
                "signal_skipped_synthetic_token_id",
                coin_id=pred.coin_id,
                symbol=pred.symbol,
                signal_type="narrative_prediction",
                reason="resolution_check_error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue
        if not resolves:
            logger.info(
                "signal_skipped_synthetic_token_id",
                coin_id=pred.coin_id,
                symbol=pred.symbol,
                signal_type="narrative_prediction",
                reason="token_id_not_in_price_cache_or_snapshots",
            )
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
                symbol=pred.symbol,  # BL-076 Bug 2 fix
                name=pred.name,  # BL-076 Bug 2 fix
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
        skipped_junk = 0
        for c in new_chains:
            # chain_matches has no symbol column, so the ticker half of
            # _is_tradeable_candidate can't run here. We apply the two
            # coin_id-side checks (wrapped/bridged prefix + non-ASCII slug);
            # ASCII-coin_id tokens that carry a non-ASCII ticker will still
            # leak through this path until BL-061 propagates symbol into
            # chain_matches. Do not claim upstream filters close this gap —
            # chain_matches is populated from many event sources, not just
            # the 6 now-filtered dispatchers.
            token_id = c["token_id"]
            if (
                not isinstance(token_id, str)
                or not token_id
                or _is_junk_coinid(token_id)
                or not token_id.isascii()
            ):
                skipped_junk += 1
                logger.warning(
                    "signal_skipped_junk",
                    coin_id=token_id,
                    signal_type="chain_completed",
                )
                continue
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
                # BL-076: chain_matches has no symbol/name — resolve via
                # Database.lookup_symbol_name_by_coin_id (sequential lookup
                # gainers_snapshots → volume_history_cg → volume_spikes).
                # Returns ("", "") for orphan coins; log a warning so
                # operator sees the gap rate. open_trade still fires —
                # the trade is real, we just lack metadata.
                #
                # SF-2 fix (PR #67 silent-failure-hunter): wrap the lookup
                # in its own try/except so an unexpected exception
                # (e.g. aiosqlite.ProgrammingError) doesn't get caught by
                # the dispatcher's outer `except Exception` and silently
                # drop the trade. Lookup error → degrade metadata, NOT
                # block the trade.
                try:
                    ct_symbol, ct_name = await db.lookup_symbol_name_by_coin_id(
                        c["token_id"]
                    )
                except Exception:
                    logger.exception(
                        "chain_metadata_lookup_failed",
                        token_id=c["token_id"],
                    )
                    ct_symbol, ct_name = "", ""
                ct_orphan = not ct_symbol and not ct_name
                if ct_orphan:
                    logger.warning(
                        "chain_completed_no_metadata",
                        coin_id=c["token_id"],
                        hint="no row in gainers_snapshots/volume_history_cg/volume_spikes",
                    )
                # MF-2 fix: pass expected_empty_metadata=True when we
                # KNOW we have no metadata (orphan path) so the engine
                # WARNING+INFO doesn't double-fire. The dispatcher already
                # logged chain_completed_no_metadata above; the engine
                # event would be wallpaper noise indistinguishable from
                # a 4th-dispatcher caller-drift bug (which is what F4 is
                # supposed to surface).
                await engine.open_trade(
                    token_id=c["token_id"],
                    symbol=ct_symbol,
                    name=ct_name,
                    chain="coingecko",
                    signal_type="chain_completed",
                    signal_data={
                        "pattern": c["pattern_name"],
                        "boost": c["conviction_boost"],
                    },
                    entry_price=chain_price,
                    signal_combo=combo_key,
                    expected_empty_metadata=ct_orphan,
                )
            except Exception:
                logger.exception("trading_chain_error", token_id=c["token_id"])
        if new_chains:
            logger.info(
                "trade_chain_completions_filtered",
                total=len(new_chains),
                skipped_junk=skipped_junk,
            )
    except Exception:
        logger.exception("trading_chain_complete_error")
