# scout/counter/prompts.py
"""Static prompts for counter-narrative scoring. Never modified by the agent."""

COUNTER_SYSTEM = (
    "You are a risk analyst evaluating crypto trades. You receive objective red flags "
    "with pre-computed severities. Your job: synthesize these flags into a risk assessment. "
    "Do NOT add new flags or change severities — they are computed from data. "
    "Return ONLY valid JSON."
)

COUNTER_NARRATIVE_TEMPLATE = """\
Token: {token_name} ({symbol}), ${market_cap:,.0f} mcap, {price_change_24h:+.1f}% 24h
Category: {category_name} (accelerating: {acceleration:+.1f}%)
Narrative fit score (bullish): {narrative_fit_score}/100
Data completeness: {data_completeness}

PRE-COMPUTED RED FLAGS (ground truth — do not modify):
{formatted_flags}

SCORING SCALE:
0-20: No identifiable risk. All data points look healthy.
21-40: Minor concerns that don't invalidate the thesis.
41-60: Meaningful risk — one or more flags warrant caution.
61-80: Strong evidence against this trade. Multiple high-severity flags.
81-100: Clear red flags — high probability of loss.

Based on the red flags above, assign a risk_score and write a 1-2 sentence \
counter_argument explaining why this trade might fail. If there are no red flags, \
assign risk_score 0-20 and note the absence of concerns.

Return ONLY JSON:
{{"risk_score": <int 0-100>, "counter_argument": "<1-2 sentences>"}}"""

COUNTER_MEMECOIN_TEMPLATE = """\
Token: {token_name} ({symbol}) on {chain}
Age: {token_age_hours:.0f} hours, Liquidity: ${liquidity:,.0f}, Volume/Liq: {vol_liq_ratio:.1f}x
Buy pressure: {buy_pressure:.0%}, Holders: {holder_count}
Data completeness: {data_completeness}

PRE-COMPUTED RED FLAGS (ground truth — do not modify):
{formatted_flags}

SCORING SCALE:
0-20: No identifiable risk. All data points look healthy.
21-40: Minor concerns that don't invalidate the thesis.
41-60: Meaningful risk — one or more flags warrant caution.
61-80: Strong evidence against this trade. Multiple high-severity flags.
81-100: Clear red flags — high probability of loss or rug.

Based on the red flags above, assign a risk_score and write a 1-2 sentence \
counter_argument explaining why this trade might fail.

Return ONLY JSON:
{{"risk_score": <int 0-100>, "counter_argument": "<1-2 sentences>"}}"""


def format_flags_for_prompt(flags: list) -> str:
    """Format RedFlag list as text for LLM prompt."""
    if not flags:
        return "(no red flags detected)"
    lines = []
    for f in flags:
        lines.append(f"- [{f.severity.upper()}] {f.flag}: {f.detail}")
    return "\n".join(lines)
