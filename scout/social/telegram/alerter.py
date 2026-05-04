"""BL-064 alerter — two-tier provenance template.

Closes the trust-laundering risk flagged by the devil's-advocate review:
tg_social alerts MUST visually telegraph "curator-sourced, unverified by
us" so the operator doesn't manually mirror live based on the alert
alone. Pipeline-sourced alerts (other signal types) keep their existing
format.
"""

from __future__ import annotations

import structlog

from scout.social.telegram.models import (
    ContractRef,
    ResolutionResult,
    ResolutionState,
    ResolvedToken,
)

log = structlog.get_logger()


_SAFETY_BADGE_VERIFIED = "✅ verified (GoPlus)"
_SAFETY_BADGE_FAILED = "❌ FAILED safety check"
_SAFETY_BADGE_UNKNOWN = "⚠️ safety unknown (GoPlus unreachable)"
_SAFETY_BADGE_NO_CA = "⊘ no CA — safety check skipped"


def _safety_badge(token) -> str:
    """Pick the right badge so cashtag-only posts don't masquerade as
    'FAILED safety check' (devil's advocate IMPORTANT #4)."""
    if getattr(token, "safety_skipped_no_ca", False):
        return _SAFETY_BADGE_NO_CA
    if not token.safety_check_completed:
        return _SAFETY_BADGE_UNKNOWN
    return _SAFETY_BADGE_VERIFIED if token.safety_pass else _SAFETY_BADGE_FAILED


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    if v >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:,.2f}"


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1:
        return f"${v:,.4f}"
    if v >= 0.01:
        return f"${v:.6f}"
    return f"${v:.8f}"


def _shorten_ca(addr: str) -> str:
    if len(addr) <= 14:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


def format_resolved_alert(
    *,
    channel_handle: str,
    cashtags: list[str],
    token: ResolvedToken,
    paper_trade_id: int | None,
    blocked_gate: str | None = None,
    msg_link: str | None = None,
) -> str:
    """Resolved-token tg_social alert. Two-tier provenance template.

    Always opens with the [CURATOR SIGNAL — VERIFY BEFORE MANUAL ACTION]
    banner. Closes with an explicit reminder that this is single-source.
    """
    safety = _safety_badge(token)
    cashtag_str = ", ".join(f"${t}" for t in cashtags) if cashtags else "—"
    ca_str = (
        f"{token.chain}, CA: {_shorten_ca(token.contract_address)}"
        if token.contract_address
        else "(no CA — ticker-only)"
    )

    if paper_trade_id is not None:
        action_line = f"[ TRADE_DISPATCHED paper id={paper_trade_id} ]"
    elif blocked_gate is not None:
        action_line = f"[ ALERT-ONLY — blocked by gate: {blocked_gate} ]"
    elif token.contract_address is None:
        action_line = "[ ALERT-ONLY — ticker-only resolution ]"
    else:
        action_line = "[ ALERT-ONLY ]"

    body = (
        f"⚠️ [CURATOR SIGNAL — VERIFY BEFORE MANUAL ACTION] ⚠️\n"
        f"{channel_handle} posted {cashtag_str}\n"
        f"Resolved: {token.symbol} ({ca_str})\n"
        f"Mcap: {_fmt_money(token.mcap)} | "
        f"Price: {_fmt_price(token.price_usd)} | "
        f"Vol 24h: {_fmt_money(token.volume_24h_usd)}\n"
        f"Safety: {safety}\n"
        f"{action_line}\n"
    )
    if msg_link:
        body += f"🔗 {msg_link}\n"
    body += (
        "─────\n"
        "This is a single-curator signal, NOT a multi-source pipeline confirmation. "
        "Independent verification required before any live action."
    )
    return body


def format_candidates_alert(
    *,
    channel_handle: str,
    cashtags: list[str],
    candidates: list[ResolvedToken],
    msg_link: str | None = None,
    paper_trade_id: int | None = None,  # BL-065 v3
    blocked_gate: str | None = None,  # BL-065 v3
) -> str:
    """Cashtag-only alert with top-3 candidates surfaced for manual disambig.

    BL-065 v3 (2026-05-04): when channel has cashtag_trade_eligible=1, the
    listener dispatches top-1 via dispatch_cashtag_to_engine. Adds:
    - paper_trade_id: opened trade ID (None if not dispatched / blocked)
    - blocked_gate: dispatcher's gate name when admission blocked (None if dispatched)
    """
    cashtag_str = ", ".join(f"${t}" for t in cashtags) if cashtags else "—"
    # BL-065 v3 fix the hardcoded 'auto-trade disabled' line — show actual outcome.
    if paper_trade_id is not None:
        outcome_line = (
            f"Top-3 candidates by mcap "
            f"(✅ DISPATCHED top-1 as paper_trade_id={paper_trade_id}):"
        )
    elif blocked_gate is not None:
        outcome_line = (
            f"Top-3 candidates by mcap "
            f"(🚫 cashtag dispatch blocked: {blocked_gate}):"
        )
    else:
        outcome_line = "Top-3 candidates by mcap (no CA in the post — alert-only):"
    lines = [
        "⚠️ [CURATOR SIGNAL — TICKER-ONLY, VERIFY MANUALLY] ⚠️",
        f"{channel_handle} posted {cashtag_str}",
        outcome_line,
    ]
    for i, c in enumerate(candidates, start=1):
        lines.append(
            f"  {i}. {c.symbol}  mcap={_fmt_money(c.mcap)}  price={_fmt_price(c.price_usd)}"
        )
    if msg_link:
        lines.append(f"🔗 {msg_link}")
    lines.append("─────")
    if paper_trade_id is not None:
        lines.append(
            f"Top-1 candidate auto-dispatched to paper trade {paper_trade_id}. "
            "Verify on dashboard if the candidate matches curator intent."
        )
    elif blocked_gate is not None:
        lines.append(
            f"Cashtag dispatch admission denied at gate '{blocked_gate}'. "
            "See journalctl tg_social_cashtag_admission_blocked for reason."
        )
    else:
        lines.append(
            "Resolved by ticker only — could be wrong token. "
            "Verify CA on the source post before any action."
        )
    return "\n".join(lines)


def format_unresolved_alert(
    *,
    channel_handle: str,
    cashtags: list[str],
    contracts: list[ContractRef],
    state: ResolutionState,
    msg_link: str | None = None,
) -> str:
    """When we couldn't resolve anything — surface the raw extracts so the
    operator can investigate manually. State badge differentiates transient
    (might resolve in 60s) vs terminal (gave up after retry)."""
    badge = (
        "[unresolved — retry pending]"
        if state == ResolutionState.UNRESOLVED_TRANSIENT
        else "[unresolved — terminal]"
    )
    lines = [
        f"⚠️ [CURATOR SIGNAL — UNRESOLVED] {badge} ⚠️",
        f"{channel_handle}:",
    ]
    if cashtags:
        lines.append(f"  cashtags: {', '.join(f'${t}' for t in cashtags)}")
    if contracts:
        lines.append("  contracts:")
        for c in contracts:
            lines.append(f"    {c.chain}: {c.address}")
    if msg_link:
        lines.append(f"🔗 {msg_link}")
    return "\n".join(lines)
