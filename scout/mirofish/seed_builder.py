"""Build MiroFish simulation seed payloads from CandidateToken data."""

from scout.models import CandidateToken


def build_seed(
    token: CandidateToken,
    signals_fired: list[str] | None = None,
    signal_confidence: str | None = None,
) -> dict:
    """Build a simulation seed document for MiroFish.

    Returns a structured dict with token metadata and a formatted prompt
    string matching PRD Section 8.2 seed format.
    """
    age_hours = int(token.token_age_days * 24)
    social = (
        f"{token.social_mentions_24h} mentions in 24h"
        if token.social_mentions_24h > 0
        else "None detected"
    )
    concept_description = (
        f"{token.token_name} ({token.ticker}) is a {token.chain} token"
    )

    # Build quantitative context
    quant_lines = []
    if token.volume_24h_usd > 0:
        quant_lines.append(f"24h volume: ${token.volume_24h_usd:,.0f}")
    if token.liquidity_usd > 0:
        quant_lines.append(f"Liquidity: ${token.liquidity_usd:,.0f}")
    if token.price_change_1h is not None:
        quant_lines.append(f"1h price change: {token.price_change_1h:+.1f}%")
    if token.price_change_24h is not None:
        quant_lines.append(f"24h price change: {token.price_change_24h:+.1f}%")
    if token.holder_count > 0:
        quant_lines.append(f"Holders: {token.holder_count}")
    quant_context = " | ".join(quant_lines) if quant_lines else "No quantitative data"

    prompt = (
        f"Token: {token.token_name} ({token.ticker}) on {token.chain}. "
        f"Market cap: ${token.market_cap_usd:,.0f}. "
        f"First seen: {age_hours}h ago. "
        f"Quantitative signals: {quant_context}. "
        f"Social signals: {social}. "
    )
    if signals_fired:
        prompt += f"Fired quantitative signals: {', '.join(signals_fired)}. "
    if signal_confidence:
        prompt += f"Signal confidence: {signal_confidence}. "
    prompt += (
        "Score the viral narrative potential of this token for crypto Twitter "
        "and Telegram communities over the next 24 hours."
    )

    seed = {
        "token_name": token.token_name,
        "ticker": token.ticker,
        "chain": token.chain,
        "market_cap": token.market_cap_usd,
        "age_hours": age_hours,
        "concept_description": concept_description,
        "social_snippets": social,
        "prompt": prompt,
    }

    if signals_fired is not None:
        seed["signals_fired"] = signals_fired
    if signal_confidence is not None:
        seed["signal_confidence"] = signal_confidence

    return seed
