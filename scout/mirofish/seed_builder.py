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
    social = f"{token.social_mentions_24h} mentions in 24h" if token.social_mentions_24h > 0 else "None detected"
    concept_description = f"{token.token_name} ({token.ticker}) is a {token.chain} token"

    prompt = (
        f"Token: {token.token_name} ({token.ticker}) on {token.chain}. "
        f"Concept: {concept_description}. "
        f"Market cap: ${token.market_cap_usd}. "
        f"First seen: {age_hours}h ago. "
        f"Early social signals: {social}. "
        f"Predict: will this narrative spread organically through crypto Twitter "
        f"and Telegram communities over the next 24 hours?"
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
