# scout/narrative/prompts.py
"""Static base prompts for narrative rotation agent. Never modified by the agent."""

NARRATIVE_FIT_SYSTEM = (
    "You are a crypto narrative analyst evaluating whether specific tokens "
    "fit an accelerating category trend. Score objectively based on data provided. "
    "Return ONLY valid JSON with no other text."
)

NARRATIVE_FIT_TEMPLATE = """\
Category "{category_name}" is accelerating: market cap {mcap_change}% in 24h \
(acceleration: {acceleration}%), volume ${volume:,.0f} (+{vol_growth}% in 6h).
Category leaders: {top_3_coins}.

Evaluate {token_name} ({symbol}, ${market_cap:,.0f} mcap, {price_change_24h:+.1f}% 24h):
Objective data: market regime: {market_regime}, \
category coin count change: {coin_count_change}, token volume/mcap ratio: {vol_mcap_ratio:.2f}.
CoinGecko watchlist: {watchlist_users:,} users tracking this coin (>10k = mainstream, >100k = high interest, >1M = viral)

1. Does this token genuinely belong to the {category_name} narrative?
2. Given the objective data above, is the volume/price trend consistent with genuine accumulation?
3. Cultural staying power: is this narrative a 1-day catalyst or multi-week trend?
4. Risk factors: any red flags in the data?

{lessons_appendix}\
Return ONLY JSON:
{{"narrative_fit": <int 0-100>, "staying_power": "<Low|Medium|High>", \
"confidence": "<Low|Medium|High>", "reasoning": "<2-3 sentences>"}}"""

DAILY_REFLECTION_TEMPLATE = """\
You are the strategy advisor for a crypto narrative rotation agent.
Review these predictions and their outcomes.

PREDICTIONS AND OUTCOMES (last {sample_size}):
{predictions_json}

CONTROL BASELINE: {control_hit_rate:.1f}% (random picks from same pool)
AGENT HIT RATE: {agent_hit_rate:.1f}%
TRUE ALPHA: {true_alpha:.1f}% (target: >10pp above baseline)

CURRENT STRATEGY:
{strategy_json}

MARKET REGIME BREAKDOWN:
{regime_breakdown}

COUNTER-RISK HIT RATES (pre-aggregated):
{counter_summary}

Analyze:
1. Which categories produced the most HITs vs MISSes?
2. Did narrative_fit_score correlate with outcomes?
3. Are thresholds too tight or too loose?
4. Timing: do 6h outcomes differ from 48h? What's peak vs 48h?
5. Does trigger_count correlate with better outcomes?
6. Market regime: should thresholds differ in BULL vs BEAR vs CRAB?
7. Survivorship: did categories with negative coin_count_change produce more MISSes?
8. Does counter_risk_score correlate with outcomes? Do high counter scores produce more MISSes?

Suggest 0-3 strategy adjustments:
{{"key": "<strategy_key>", "new_value": <value>, "reason": "<citing data>"}}

IMPORTANT: Only suggest changes supported by data. "No changes" is valid.
Return JSON: {{"adjustments": [...], "reflection": "<3-5 sentences>", \
"true_alpha": <float>, "regime_insight": "<1 sentence>"}}"""

WEEKLY_CONSOLIDATION_TEMPLATE = """\
Here are the lessons appended to the narrative scoring prompt:
{current_lessons}

This week's daily reflections:
{weekly_reflections}

CONTRARIAN CHECK: Do not validate your own prior reasoning.
For each lesson, check hit rate BEFORE and AFTER introduction:
{hit_rate_per_lesson}
If a lesson did not improve hit rate by >3pp, REMOVE it.

Consolidate into max 10 lessons. Remove:
- Lessons where hit rate did not improve (data-driven)
- Contradictory lessons (keep one with better hit rate)
- Redundant lessons (merge)

Return JSON: {{"consolidated_lessons": "<max 10 bullet points>", \
"lessons_version": {next_version}, \
"removed": [{{"lesson": "<text>", "reason": "<why>", \
"hit_rate_before": 0, "hit_rate_after": 0}}]}}"""
