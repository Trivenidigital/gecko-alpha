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

BL-065 v3 (2026-05-04): adds sibling cashtag dispatcher — see
`dispatch_cashtag_to_engine` and `_evaluate_cashtag` below. Cashtag-specific
gate set (no_ca/safety skipped by design; cashtag_disabled/no_candidates/
below_floor/ambiguous/channel_rate_limited added).
"""

from __future__ import annotations

import asyncio

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


async def _channel_safety_required(db: Database, channel_handle: str) -> bool:
    """True iff the channel requires GoPlus safety check completion before
    trade. Defaults True (strict / fail-closed) on missing row or NULL —
    the per-channel opt-in to lenient is explicit, never implicit.

    Lenient channels still get the safety check run; only the
    no-record/timeout/5xx path is permitted to pass through. Honeypot
    or high-tax verdicts ALWAYS block, regardless of this flag.
    """
    cur = await db._conn.execute(
        "SELECT safety_required FROM tg_social_channels "
        "WHERE channel_handle = ? AND removed_at IS NULL",
        (channel_handle,),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return True
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

    # Gate 4 part A: safety check must have COMPLETED — but per-channel
    # `safety_required=False` lets trusted curators bypass this for fresh
    # tokens (e.g. Pump.fun memes minted ~30min ago that GoPlus hasn't
    # indexed yet). The lenient path still passes through Gate 4 part B,
    # so a CONFIRMED honeypot/high-tax verdict still blocks regardless.
    if not token.safety_check_completed:
        if await _channel_safety_required(db, channel_handle):
            return AdmissionDecision(
                dispatch_trade=False,
                blocked_gate="safety_unknown",
                reason="GoPlus safety check did not complete (5xx/timeout/no-record)",
            )
        log.info(
            "tg_social_safety_unknown_lenient_pass",
            channel_handle=channel_handle,
            token_id=token.token_id,
            symbol=token.symbol,
            note="channel.safety_required=0 — trusting curator on no-record",
        )
    # Gate 4 part B: safety verdict must PASS — applies even on lenient
    # channels. Honeypot/high-tax is a definitive verdict, not a no-record.
    if token.safety_check_completed and not token.safety_pass:
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
) -> tuple[int | None, str | None]:
    """Execute admission then call TradingEngine.open_trade. Returns
    (paper_trade_id, blocked_gate) — the gate name is captured FROM THE
    SAME evaluate() call that decided rejection, eliminating the TOCTOU
    race the previous double-evaluate created (silent-failure HIGH#4).

    On engine-side rejection the gate is reported as 'engine_*' so the
    operator can correlate with the engine's own log line.
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
        return (None, decision.blocked_gate)

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
        return (trade_id, None)

    # Engine-side gate rejected; engine logs the specific reason. We tag
    # generically so the alerter can render "blocked by engine".
    log.info(
        "tg_social_admission_blocked_engine",
        token_id=token.token_id,
        symbol=token.symbol,
        channel_handle=channel_handle,
        note="see engine log for specific gate (warmup/quota/exposure/mcap/junk/cooldown)",
    )
    return (None, "engine_rejected")


# ---------------------------------------------------------------------------
# BL-065 v3 (Bundle B 2026-05-04): cashtag-only dispatch path
# ---------------------------------------------------------------------------


async def _channel_cashtag_trade_eligible(db: Database, channel_handle: str) -> bool:
    """BL-065: per-channel opt-in for cashtag dispatch. Fail-closed default
    (returns False on missing row, NULL, or 0 — explicit 1 required).

    Independent of trade_eligible (the CA-path flag) — operator may want
    a channel to dispatch CAs without dispatching cashtags, or vice versa.
    """
    cur = await db._conn.execute(
        "SELECT cashtag_trade_eligible FROM tg_social_channels "
        "WHERE channel_handle = ? AND removed_at IS NULL",
        (channel_handle,),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return False
    return bool(row[0])


async def _channel_cashtag_trades_today_count(db: Database, channel_handle: str) -> int:
    """R1#5 v2: count cashtag-resolution paper_trades opened today by channel.

    Per design F2.1 v3: SCAN within indexed (signal_combo, opened_at) prefix;
    json_extract is per-row Python call within that narrowed scan. Sub-ms at
    current cardinality (5-50 same-day tg_social rows in prod).
    """
    cur = await db._conn.execute(
        """
        SELECT COUNT(*) FROM paper_trades
         WHERE signal_type = 'tg_social'
           AND json_extract(signal_data, '$.channel_handle') = ?
           AND json_extract(signal_data, '$.resolution') = 'cashtag'
           AND opened_at >= datetime('now', 'start of day')
        """,
        (channel_handle,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _check_potential_symbol_duplicate(
    db: Database, token_id: str, symbol: str
) -> list[tuple[str, int]]:
    """R1#6 v2 / R2#5 v3: returns list of (token_id, paper_trade_id) collisions
    instead of logging per-row. Caller decides log level (INFO single vs
    aggregate WARNING).

    Cross-coin_id dedup gap mitigation: when a cashtag dispatch opens a trade
    for token_id=X symbol=Y, return any OTHER currently-open tg_social trade
    with same SYMBOL Y but different token_id (e.g., 'pepe' vs 'pepe-bsc' —
    same memecoin across chains, different CoinGecko coin_ids).
    """
    cur = await db._conn.execute(
        """
        SELECT s.token_id, p.id
          FROM tg_social_signals s
          JOIN paper_trades p ON s.paper_trade_id = p.id
         WHERE p.status = 'open'
           AND p.signal_type = 'tg_social'
           AND UPPER(s.symbol) = UPPER(?)
           AND s.token_id != ?
         LIMIT 5
        """,
        (symbol, token_id),
    )
    return [(r[0], r[1]) for r in await cur.fetchall()]


async def _evaluate_cashtag(
    *,
    db: Database,
    settings: Settings,
    candidates: list[ResolvedToken],
    channel_handle: str,
) -> AdmissionDecision:
    """BL-065 v3: cashtag-specific gates.

    Skipped (vs. CA path):
      * Gate 2 no_ca — by definition no CA; skipping is the whole point
      * Gate 4 safety — no CA = no GoPlus; operator opts into this risk
        explicitly via cashtag_trade_eligible=1

    Added:
      * cashtag_disabled — channel.cashtag_trade_eligible=0
      * cashtag_no_candidates — empty candidates_top3 (R2#3 v3)
      * cashtag_below_floor — top.mcap < PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD
      * cashtag_ambiguous — len>1 AND top.mcap < second.mcap × DISAMBIGUITY_RATIO
      * cashtag_channel_rate_limited — per-channel daily cap (R1#5 v2)

    Reused (from CA path):
      * dedup_open — per-OPEN-exposure dedup by token_id (shared semantic)
      * tg_social_quota — same TG_SOCIAL_MAX_OPEN_TRADES global cap
    """
    # Gate A: channel cashtag opt-in
    if not await _channel_cashtag_trade_eligible(db, channel_handle):
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="cashtag_disabled",
            reason="tg_social_channels.cashtag_trade_eligible=0 (default)",
        )

    # R2#3 v3: empty candidates is a distinct upstream-resolver problem,
    # NOT a channel configuration issue.
    if not candidates:
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="cashtag_no_candidates",
            reason="resolver returned empty candidates_top3 (upstream issue)",
        )
    top = candidates[0]

    # Gate B: mcap floor (skip dust)
    min_mcap = settings.PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD
    if (top.mcap or 0) < min_mcap:
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="cashtag_below_floor",
            reason=f"top candidate mcap {top.mcap} < floor {min_mcap}",
        )

    # Gate C: disambiguity (top must clearly dominate #2)
    if len(candidates) > 1:
        second_mcap = candidates[1].mcap or 0
        ratio_required = settings.PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO
        if second_mcap > 0 and (top.mcap or 0) < second_mcap * ratio_required:
            return AdmissionDecision(
                dispatch_trade=False,
                blocked_gate="cashtag_ambiguous",
                reason=(
                    f"top mcap {top.mcap} < {ratio_required}x second mcap "
                    f"{second_mcap} - possible look-alike token"
                ),
            )

    # Gate D: per-OPEN-exposure dedup (shared with CA path)
    if await _has_open_tg_social_exposure(db, top.token_id):
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="dedup_open",
            reason="another tg_social trade is currently open on this token",
        )

    # Gate E: tg_social slot quota (global)
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

    # Gate F (R1#5 v2): per-channel daily cashtag-dispatch rate cap.
    # Cashtag dispatch bypasses GoPlus, so blast radius of one bad/noisy
    # curator is higher than CA path.
    today_count = await _channel_cashtag_trades_today_count(db, channel_handle)
    if today_count >= settings.PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY:
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="cashtag_channel_rate_limited",
            reason=(
                f"channel {channel_handle} cashtag trades today {today_count} "
                f">= cap {settings.PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY}"
            ),
        )

    return AdmissionDecision(dispatch_trade=True)


async def dispatch_cashtag_to_engine(
    *,
    db: Database,
    settings: Settings,
    engine: TradingEngine,
    candidates: list[ResolvedToken],
    cashtag: str,  # e.g. "EITHER" — already normalized (no '$')
    channel_handle: str,
) -> tuple[int | None, str | None]:
    """BL-065 v3: dispatch top-1 cashtag candidate to TradingEngine.open_trade.

    Returns (paper_trade_id, blocked_gate). signal_data carries the
    cashtag-resolution provenance fields per BL-065 acceptance:
    {"resolution": "cashtag", "cashtag": "$X", "candidate_rank": 1,
     "candidates_total": N}.

    On any rejection, returns (None, gate_name). On engine-side rejection,
    gate is 'engine_rejected' (engine logs specific reason).

    R1-M2 v3: symbol-collision check is wrapped in nested try/except so
    helper failure does NOT escape the dispatcher (trade is already open).
    """
    decision = await _evaluate_cashtag(
        db=db,
        settings=settings,
        candidates=candidates,
        channel_handle=channel_handle,
    )
    if not decision.dispatch_trade:
        log.info(
            "tg_social_cashtag_admission_blocked",
            cashtag=cashtag,
            candidates_total=len(candidates),
            channel_handle=channel_handle,
            gate_name=decision.blocked_gate,
            reason=decision.reason,
        )
        return (None, decision.blocked_gate)

    top = candidates[0]
    trade_id = await engine.open_trade(
        token_id=top.token_id,
        symbol=top.symbol,
        name=top.symbol,
        chain=top.chain or "coingecko",
        signal_type="tg_social",
        signal_data={
            "channel_handle": channel_handle,
            "resolution": "cashtag",
            "cashtag": f"${cashtag}",
            "candidate_rank": 1,
            "candidates_total": len(candidates),
            "mcap_at_sighting": top.mcap,
        },
        amount_usd=settings.PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD,
        entry_price=top.price_usd,
        signal_combo="tg_social",
    )
    if trade_id is not None:
        # R1-M2 v3 + R2#5 v3: nested guard — helper failure must NOT escape.
        # Trade is already open; lifecycle owns it.
        try:
            collisions = await _check_potential_symbol_duplicate(
                db, top.token_id, top.symbol
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            log.exception(
                "tg_social_symbol_collision_check_failed",
                paper_trade_id=trade_id,
                token_id=top.token_id,
                note="trade is open; collision check skipped this dispatch",
            )
            collisions = []
        if collisions:
            # R2#5 v3: INFO not WARNING — symbol collisions are routine on
            # memecoins (every chain has its own PEPE).
            log.info(
                "tg_social_potential_duplicate_symbol",
                paper_trade_id=trade_id,
                new_token_id=top.token_id,
                symbol=top.symbol,
                colliding_token_ids=[c[0] for c in collisions],
                colliding_paper_trade_ids=[c[1] for c in collisions],
                note=(
                    "open tg_social trade(s) exist with same SYMBOL but "
                    "different token_id - possible cross-listing duplicate. "
                    "Trade NOT blocked; full per-symbol dedup deferred to BL-065'."
                ),
            )
        log.info(
            "tg_social_cashtag_trade_dispatched",
            paper_trade_id=trade_id,
            token_id=top.token_id,
            symbol=top.symbol,
            cashtag=f"${cashtag}",
            candidates_total=len(candidates),
            amount_usd=settings.PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD,
            channel_handle=channel_handle,
        )
        return (trade_id, None)

    log.info(
        "tg_social_cashtag_admission_blocked_engine",
        token_id=top.token_id,
        symbol=top.symbol,
        cashtag=f"${cashtag}",
        channel_handle=channel_handle,
        note="see engine log for specific gate",
    )
    return (None, "engine_rejected")
