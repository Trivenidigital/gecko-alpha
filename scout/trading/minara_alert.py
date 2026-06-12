"""BL-NEW-M1.5C: Minara DEX-eligibility alert extension.

When a TG paper-trade-open alert is about to fire for a Solana-listed
token, this module prepares a formatted `minara swap` shell command that
the operator copy-pastes into their local terminal where Minara is logged
in. gecko-alpha does NOT execute the command. Durable emission logging and
DB persistence happen after Telegram delivery succeeds, so generated-but-
undelivered commands do not count as operator-visible Minara emissions.
"""

from __future__ import annotations

import asyncio

import structlog

from scout.config import Settings
from scout.counter.detail import fetch_coin_detail

log = structlog.get_logger(__name__)

# Solana SPL addresses are base58-encoded Ed25519 public keys: exactly
# 32 bytes encoded to 32-44 chars from this alphabet. Reject corrupt CG
# entries where an EVM-shaped address appears under the solana platform.
_BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _looks_like_spl_address(s: str) -> bool:
    """True if `s` is plausibly a Solana SPL address."""
    if not (32 <= len(s) <= 44):
        return False
    return all(c in _BASE58_ALPHABET for c in s)


def minara_alert_amount_usd(settings: Settings) -> int:
    """Settings-sourced Minara size, clamped to a positive integer."""
    size = getattr(settings, "MINARA_ALERT_AMOUNT_USD", 10.0)
    try:
        return max(1, int(round(float(size))))
    except (TypeError, ValueError):
        return 10


def minara_source_event_id(tg_alert_log_id: int | None) -> str | None:
    if tg_alert_log_id is None:
        return None
    return f"tg_alert_log:{tg_alert_log_id}"


def log_minara_alert_command_emitted(
    *,
    coin_id: str,
    chain: str,
    amount_usd: int,
    source_event_id: str | None,
) -> None:
    """Emit the operator-visible Minara command log after Telegram delivery."""
    log.info(
        "minara_alert_command_emitted",
        coin_id=coin_id,
        chain=chain,
        amount_usd=amount_usd,
        source_event_id=source_event_id,
    )


async def persist_minara_alert_emission(
    *,
    db,
    paper_trade_id: int | None,
    signal_type: str | None,
    tg_alert_log_id: int | None,
    coin_id: str,
    chain: str,
    amount_usd: int,
    command_text: str,
    persistence_lock_timeout_sec: float = 0.25,
) -> None:
    """Best-effort persistence for an already delivered Minara alert."""
    source_event_id = minara_source_event_id(tg_alert_log_id)
    if db is None or signal_type is None:
        log.info(
            "minara_alert_emission_persist_skipped",
            reason="missing_db" if db is None else "missing_signal_type",
            coin_id=coin_id,
            chain=chain,
            amount_usd=amount_usd,
            source_event_id=source_event_id,
        )
        return

    try:
        inserted = await db.record_minara_alert_emission(
            paper_trade_id=paper_trade_id,
            tg_alert_log_id=tg_alert_log_id,
            signal_type=signal_type,
            coin_id=coin_id,
            chain=chain,
            amount_usd=amount_usd,
            command_text=command_text,
            source_event_id=source_event_id,
            lock_timeout_sec=persistence_lock_timeout_sec,
        )
        log.info(
            (
                "minara_alert_emission_persisted"
                if inserted
                else "minara_alert_emission_persist_duplicate_ignored"
            ),
            coin_id=coin_id,
            chain=chain,
            amount_usd=amount_usd,
            source_event_id=source_event_id,
        )
    except asyncio.TimeoutError:
        log.warning(
            "minara_alert_emission_persist_timeout",
            coin_id=coin_id,
            chain=chain,
            amount_usd=amount_usd,
            source_event_id=source_event_id,
        )
    except Exception as exc:
        log.warning(
            "minara_alert_emission_persist_failed",
            coin_id=coin_id,
            chain=chain,
            amount_usd=amount_usd,
            source_event_id=source_event_id,
            err=str(exc),
            err_type=type(exc).__name__,
        )


def _build_swap_command(settings: Settings, spl_address: str) -> str:
    """Build the Minara swap command line for a validated SPL address."""
    from_token = getattr(settings, "MINARA_ALERT_FROM_TOKEN", "USDC")
    size_int = minara_alert_amount_usd(settings)
    return (
        f"minara swap --from {from_token} --to {spl_address} "
        f"--amount-usd {size_int}"
    )


async def maybe_minara_command(
    session,
    settings: Settings,
    coin_id: str,
    amount_usd: float | None,
) -> str | None:
    """Return a Minara swap command for Solana-listed tokens.

    Returns None for non-Solana, disabled, fetch-failed, or malformed cases.
    Never catches asyncio.CancelledError.
    """
    del amount_usd  # Paper-trade size is intentionally not used.
    if not getattr(settings, "MINARA_ALERT_ENABLED", True):
        return None

    # BL-NEW-MINARA-SOLANA-NATIVE-ID: chain='solana' tokens carry the SPL
    # contract address directly as coin_id (token_id). A CG /coins/{id} lookup
    # on a raw address 404s — the regression that produced 0 emits for native
    # Solana tokens — so resolve Solana-ness from the id shape and emit
    # directly, no lookup needed. CG slugs are never 32-44 base58 chars; EVM
    # 0x… ids contain '0' (not in the base58 alphabet) so they are excluded.
    if _looks_like_spl_address(coin_id):
        return _build_swap_command(settings, coin_id)

    if session is None:
        return None

    try:
        detail = await fetch_coin_detail(
            session=session,
            coin_id=coin_id,
            api_key=getattr(settings, "COINGECKO_API_KEY", "") or "",
        )
    except Exception:
        log.exception("minara_alert_detail_fetch_failed", coin_id=coin_id)
        return None
    if not detail:
        return None

    try:
        platforms = detail.get("platforms") or {}
        if not isinstance(platforms, dict):
            return None
        spl_address = platforms.get("solana")
        if not spl_address or not isinstance(spl_address, str):
            return None
        if not _looks_like_spl_address(spl_address):
            log.info(
                "minara_alert_skipped_invalid_spl_shape",
                coin_id=coin_id,
                addr_prefix=spl_address[:8],
            )
            return None

        return _build_swap_command(settings, spl_address)
    except Exception:
        log.exception("minara_alert_format_failed", coin_id=coin_id)
        return None
