"""Telegram alert formatter for Second-Wave Detection."""

from __future__ import annotations


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v:,.0f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:.1f}%"


def format_secondwave_alert(candidate: dict) -> str:
    """Build the Telegram message for a second-wave candidate."""
    peak_signals = candidate.get("peak_signals_fired") or []
    reacc_signals = candidate.get("reaccumulation_signals") or []
    stale_marker = "(stale)" if candidate.get("price_is_stale") else ""

    lines = [
        f"\U0001f504 Second Wave Detected: {candidate.get('token_name', 'Unknown')} ({candidate.get('ticker', '')})",
        "",
        f"Prior pump (first seen {candidate.get('days_since_first_seen', 0):.1f}d ago):",
        f"  Peak score: {candidate.get('peak_quant_score', 0)}/100",
        f"  Signals: {', '.join(peak_signals) if peak_signals else 'n/a'}",
        f"  Alert market cap: {_fmt_money(candidate.get('alert_market_cap'))} (approximate peak)",
        "",
        "Cooldown:",
        f"  Drawdown from peak: {_fmt_pct(candidate.get('price_drop_from_peak_pct'))}",
        f"  Days cooling: {candidate.get('days_since_first_seen', 0):.0f}",
        "",
        "Re-accumulation:",
        f"  Price vs alert: {_fmt_pct(candidate.get('price_vs_alert_pct'))} {stale_marker}".rstrip(),
        f"  Volume vs cooldown avg: {candidate.get('volume_vs_cooldown_avg', 0):.1f}x",
        f"  Re-accumulation score: {candidate.get('reaccumulation_score', 0)}/100",
        f"  Signals: {', '.join(reacc_signals) if reacc_signals else 'n/a'}",
        "",
        f"Current: {_fmt_money(candidate.get('current_market_cap'))} mcap | {_fmt_money(candidate.get('current_volume_24h'))} vol/24h",
        "",
        f"Chain: {candidate.get('chain', '?')} | CA: {candidate.get('contract_address', '?')}",
        "",
        "RESEARCH ONLY - Not financial advice",
    ]
    return "\n".join(lines)
