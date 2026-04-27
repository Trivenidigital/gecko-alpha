"""BL-064 dispatcher — TG-only admission gates → TradingEngine.open_trade.

Only enforces gates that are TG-specific:
  1. Per-OPEN-exposure dedup (token already has open paper_trade_id from tg_social)
  2. CA-required (ticker-only never trades)
  3. channel.trade_eligible == 1
  4. Safety check completed AND verdict pass (fail-closed; closes BL-063)
  5. tg_social-specific quota (separate from main pool)

Then delegates to TradingEngine.open_trade(signal_type='tg_social', ...) which
handles the cross-cutting gates (warmup, max-open-trades global cap, max
exposure, mcap caps, junk filter, per-signal-type cooldown). This avoids the
maintenance split flagged by the architecture reviewer.
"""

from __future__ import annotations

import structlog

from scout.config import Settings
from scout.db import Database
from scout.social.telegram.models import (
    AdmissionDecision,
    ResolvedToken,
)
from scout.trading.engine import TradingEngine

log = structlog.get_logger()


async def _channel_trade_eligible(db: Database, channel_handle: str) -> bool:
    cur = await db._conn.execute(
        "SELECT trade_eligible FROM tg_social_channels "
        "WHERE channel_handle = ? AND removed_at IS NULL",
        (channel_handle,),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    return bool(row[0])


async def _has_open_tg_social_exposure(db: Database, token_id: str) -> bool:
    """Per-OPEN-exposure dedup: any tg_social_signals row whose linked
    paper_trades.status = 'open' for this token? Replaces v1's 24h-per-token
    rule per devil's-advocate IMPORTANT #6 — re-emphasis after first trade
    closes IS a valid new signal."""
    cur = await db._conn.execute(
        """
        SELECT 1
        FROM tg_social_signals s
        JOIN paper_trades p ON s.paper_trade_id = p.id
        WHERE s.token_id = ? AND p.status = 'open'
        LIMIT 1
        """,
        (token_id,),
    )
    return await cur.fetchone() is not None


async def _tg_social_open_count(db: Database) -> int:
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades "
        "WHERE signal_type = 'tg_social' AND status = 'open'"
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def evaluate(
    *,
    db: Database,
    settings: Settings,
    token: ResolvedToken,
    channel_handle: str,
) -> AdmissionDecision:
    """Run the 5 TG-only gates in order. Returns AdmissionDecision (does not
    open the trade — caller does that via TradingEngine on dispatch_trade=True)."""

    # Gate 2: CA-required (checked first — cheap)
    if not token.contract_address:
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="no_ca",
            reason="ticker-only resolution; auto-trade disabled by design",
        )

    # Gate 4 part A: safety check must have COMPLETED (fail-closed)
    if not token.safety_check_completed:
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="safety_unknown",
            reason="GoPlus safety check did not complete (5xx/timeout/no-record)",
        )
    # Gate 4 part B: safety verdict must PASS
    if not token.safety_pass:
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="safety_failed",
            reason="GoPlus flagged honeypot/blacklist/high-tax",
        )

    # Gate 3: channel.trade_eligible
    if not await _channel_trade_eligible(db, channel_handle):
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="channel_disabled",
            reason="tg_social_channels.trade_eligible=0 or channel removed",
        )

    # Gate 1: per-OPEN-exposure dedup
    if await _has_open_tg_social_exposure(db, token.token_id):
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="dedup_open",
            reason="another tg_social trade is currently open on this token",
        )

    # Gate 5: tg_social slot quota (separate from global PAPER_MAX_OPEN_TRADES)
    open_count = await _tg_social_open_count(db)
    if open_count >= settings.TG_SOCIAL_MAX_OPEN_TRADES:
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="tg_social_quota",
            reason=(
                f"tg_social open trades {open_count} "
                f">= TG_SOCIAL_MAX_OPEN_TRADES {settings.TG_SOCIAL_MAX_OPEN_TRADES}"
            ),
        )

    return AdmissionDecision(dispatch_trade=True)


async def dispatch_to_engine(
    *,
    db: Database,
    settings: Settings,
    engine: TradingEngine,
    token: ResolvedToken,
    channel_handle: str,
) -> int | None:
    """Execute admission then call TradingEngine.open_trade. Returns paper_trade_id
    on dispatch, None if any TG-only gate or engine-side gate rejected.

    Engine-side rejections (warmup, global max-open, exposure, mcap caps, junk
    filter, per-signal-type cooldown) are logged by the engine itself; we log
    the TG-only gate rejections here for symmetry.
    """
    decision = await evaluate(
        db=db, settings=settings, token=token, channel_handle=channel_handle
    )
    if not decision.dispatch_trade:
        log.info(
            "tg_social_admission_blocked",
            token_id=token.token_id,
            symbol=token.symbol,
            channel_handle=channel_handle,
            gate_name=decision.blocked_gate,
            reason=decision.reason,
        )
        return None

    trade_id = await engine.open_trade(
        token_id=token.token_id,
        symbol=token.symbol,
        name=token.symbol,
        chain=token.chain or "coingecko",
        signal_type="tg_social",
        signal_data={
            "channel_handle": channel_handle,
            "contract_address": token.contract_address,
            "mcap_at_sighting": token.mcap,
        },
        amount_usd=settings.PAPER_TG_SOCIAL_TRADE_AMOUNT_USD,
        entry_price=token.price_usd,
        signal_combo="tg_social",
    )
    if trade_id is not None:
        log.info(
            "tg_social_trade_dispatched",
            paper_trade_id=trade_id,
            token_id=token.token_id,
            symbol=token.symbol,
            amount_usd=settings.PAPER_TG_SOCIAL_TRADE_AMOUNT_USD,
            channel_handle=channel_handle,
        )
    else:
        # Engine-side gate rejected; engine already logged the specific reason.
        log.info(
            "tg_social_admission_blocked_engine",
            token_id=token.token_id,
            symbol=token.symbol,
            channel_handle=channel_handle,
            note="see engine log for specific gate (warmup/quota/exposure/mcap/junk/cooldown)",
        )
    return trade_id
