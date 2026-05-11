"""BL-NEW-M1.5C: Minara DEX-eligibility alert extension (Phase 0 Option A).

When a TG paper-trade-open alert is about to fire for a Solana-listed
token, this module returns a formatted `minara swap` shell command that
the operator copy-pastes into their local terminal where Minara is
logged in. gecko-alpha does NOT execute the command — pure decision-
support.

Architecture:
- `maybe_minara_command(session, settings, coin_id, amount_usd) -> str | None`
- Reads CoinGecko `/coins/{id}` via existing `scout.counter.detail.fetch_coin_detail`
  (30-min in-memory cache; soft-fails to None on 404/429/error)
- Detects Solana eligibility via `platforms.solana` field (non-empty SPL address)
- Returns formatted command string OR None (never raises Exception)

Failure modes (5-layer isolation):
  1. MINARA_ALERT_ENABLED=False → immediate None (no fetch)
  2. session is None → immediate None (R1-I1 design fold; no wasted
     rate-limiter acquire)
  3. fetch_coin_detail returns None (CG outage, 404, rate-limit) → None
  4. platforms non-dict or platforms.solana missing/empty → None
  5. SPL address fails base58 shape validation → None (PR-V1-I1 + V2-I2:
     guard against corrupt CG data putting EVM-shaped addresses under
     the solana key)
  6. Any unexpected Exception → caught, logged, return None
  Note: asyncio.CancelledError is NOT caught here; it propagates per
  asyncio convention. Caller (tg_alert_dispatch.notify_paper_trade_opened)
  has a try/except that demotes the pre-emptive 'sent' row to
  'dispatch_failed' so the per-token cooldown clears on cancel.

R2-C1 design fold: trade size is sourced from Settings field
MINARA_ALERT_AMOUNT_USD (default $10), NOT the caller's amount_usd
(which would be the $300 paper-trade size on prod). Operator overrides
via .env for larger sizes; default forces explicit decision.

R1-I2 design fold: size_int is clamped to ≥ 1 to avoid emitting
`--amount-usd 0` for sub-dollar Settings values.

R1-I3 design fold: amount_usd parameter is typed Optional and is
unused for size derivation (kept for API compatibility with the
notify_paper_trade_opened caller signature).

R2-I2 design fold: success-path emits structured-log event
`minara_alert_command_emitted` so operator can grep journalctl to
verify detection is working (vs. silent-never-fires regression).
"""

from __future__ import annotations

import structlog

from scout.config import Settings
from scout.counter.detail import fetch_coin_detail

log = structlog.get_logger(__name__)

# PR-V1-I1 + V2-I2 fold: Solana SPL addresses are base58-encoded Ed25519
# public keys → exactly 32 bytes encoded → 32-44 chars from this alphabet.
# Reject corrupt CG entries where an EVM hex address ("0xabc...") or
# arbitrary string ended up under the solana platforms key.
_BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _looks_like_spl_address(s: str) -> bool:
    """True if `s` is plausibly a Solana SPL address (base58, 32-44 chars).
    Defensive check against corrupt CoinGecko `platforms.solana` values
    (e.g., EVM-shaped 0x... addresses mistakenly placed under the solana
    key, observed on aggregators with sloppy normalization)."""
    if not (32 <= len(s) <= 44):
        return False
    return all(c in _BASE58_ALPHABET for c in s)


async def maybe_minara_command(
    session,
    settings: Settings,
    coin_id: str,
    amount_usd: float | None,
) -> str | None:
    """Return a Minara swap shell command for the operator if the token
    is Solana-listed. Returns None for any other case (not listed,
    fetch failed, feature disabled, session None, format error).

    Never raises.
    """
    if not getattr(settings, "MINARA_ALERT_ENABLED", True):
        return None
    # R1-I1 fold: session=None short-circuit before fetch.
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
        # PR-V1-I1 fold: non-dict platforms (CG schema drift) → None, no
        # spurious format_failed log noise.
        if not isinstance(platforms, dict):
            return None
        spl_address = platforms.get("solana")
        if not spl_address or not isinstance(spl_address, str):
            return None
        # PR-V1-I1 + V2-I2 fold: shape-validate SPL address. Prevents
        # operator pasting an EVM-shaped `Run:` line that Minara will
        # only reject server-side.
        if not _looks_like_spl_address(spl_address):
            log.info(
                "minara_alert_skipped_invalid_spl_shape",
                coin_id=coin_id,
                addr_prefix=spl_address[:8],
            )
            return None
        from_token = getattr(settings, "MINARA_ALERT_FROM_TOKEN", "USDC")
        # R2-C1 fold: Settings-sourced size (default $10), NOT caller's
        # paper-trade size. Decoupling enforces M1.5a V3-M3 discipline.
        size = getattr(settings, "MINARA_ALERT_AMOUNT_USD", 10.0)
        # R1-I2 + R1-I3 fold: clamp to int ≥ 1; handle TypeError/ValueError
        # if Settings value is mistyped at operator override time.
        try:
            size_int = max(1, int(round(float(size))))
        except (TypeError, ValueError):
            size_int = 10
        cmd = (
            f"minara swap --from {from_token} --to {spl_address} "
            f"--amount-usd {size_int}"
        )
        # R2-I2 fold: success-path log event for operator observability.
        log.info(
            "minara_alert_command_emitted",
            coin_id=coin_id,
            chain="solana",
            amount_usd=size_int,
        )
        return cmd
    except Exception:
        log.exception("minara_alert_format_failed", coin_id=coin_id)
        return None
