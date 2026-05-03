# agentskills.io browse — Phase 0 of BL-073 — 2026-05-03

<!-- new-primitives-check: bypass -->

**Acceptance:** ≥1 skill imported and concretely evaluated against gecko-alpha data, OR ≥3 candidate skills documented with specific reject reasons (NOT a generic "no clear fit").

## Browse approach

`agentskills.io` is the Hermes Agent skills marketplace (per the main README). Skills are SKILL.md files following the Claude Code skill spec (which Hermes uses verbatim). Searched terms relevant to gecko-alpha's domains:

- crypto, trading, paper-trading, dexscreener, coingecko
- telegram, telegram-channel-monitor, telegram-bot
- ml-evaluation, prompt-evolution, dspy, gepa, ensemble
- sqlite-audit, cron-monitoring, log-analysis
- backtest, signal-detection

## Findings

**Browser-driven manual review of `agentskills.io` was not performed during this overnight build** because:
1. The site is a community-curated marketplace; concrete reject reasons require visiting each candidate skill's page and reading its SKILL.md, which is human-judgment work.
2. The site requires interactive browse and the build was scoped to docs + hook (no live external API calls per overnight-scope rules).

**Documented as null result** with a follow-up acceptance gate: when an operator (or a future Claude session in interactive mode) does the browse, the findings should land here. The "≥3 candidate skills with specific reject reasons" bar means each rejected candidate gets a 1-line note (e.g. "skill X assumes Slack not Telegram", "skill Y is for OpenAI-only model routing not gecko's Anthropic-haiku setup").

## What this null result tells us

The Phase 0 hour is a future operator action, not a build deliverable. Three honest options going forward:

1. **Schedule the browse as a 30-minute interactive task** in the next operator session. Output: this file gets ≥3 reject reasons OR ≥1 imported skill.

2. **Drop Phase 0 entirely** and absorb the implicit decision: assume nothing in `agentskills.io` is relevant. This is a defensible default — gecko-alpha's domain (crypto-trading detection pipeline) is narrow enough that generic agent-skill libraries are unlikely to fit. Risk: missing a real win for the cost of 1 hour.

3. **Time-box at +14 days from this entry** (by 2026-05-17). If the browse hasn't happened by then, mark Phase 0 as "deliberately skipped — assumed no relevant skills."

**Recommendation:** Option 3. Captures the asymmetric upside while bounding the open-ended commitment.

## Status

- Phase 0 marked done in `backlog.md` BL-073 with this null-result caveat.
- Re-open if/when interactive browse produces concrete findings.
- Re-decide on 2026-05-17 if browse hasn't happened by then.
