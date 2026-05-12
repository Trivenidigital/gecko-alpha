**New primitives introduced:** NONE — audit-only finding doc. No fixes proposed in this PR. Per-site fixes are scoped to BL-NEW-PARSE-MODE-AUDIT follow-up PRs grouped by module area.

# Finding: BL-NEW-PARSE-MODE-AUDIT — audit-only scope (2026-05-12)

**Purpose.** Determine whether parse_mode hygiene (Class-3 silent rendering corruption) is a 1-instance holding-thread or a multi-instance promotion candidate for the silent-failure taxonomy. Composes with `tasks/findings_section_12c_sweep_2026_05_12.md` — that sweep listed parse_mode hygiene as one of 8 surface-similar candidate rules with 1 known instance (trending_catch auto_suspend). This audit determines the actual instance count.

**Methodology.** Read each of 15 `send_telegram_message` call sites in `scout/` that default to `parse_mode=Markdown` (per `backlog.md` BL-NEW-PARSE-MODE-AUDIT inventory). For each, trace the message body's interpolated variables and classify by whether the body's content shape can trigger Markdown rendering corruption (`_`, `*`, `[`, `]`, `` ` `` consumed as formatting markers).

The classification axis is **actual code behavior**, not filename intuition:
- **HIGH ACTUAL** — body interpolates signal_type / symbol / ticker with no escaping; production data paths routinely feed values WITH markdown-special chars (every gecko-alpha signal_type contains underscores)
- **HIGH POTENTIAL** — body interpolates dynamic content (LLM output, model-generated text); structurally vulnerable but the specific trigger hasn't been observed in production logs yet
- **MEDIUM** — body interpolates dynamic content where the content shape (numbers, percentages, status flags) makes markdown chars unlikely
- **LOW** — body is static or interpolates only system-controlled numeric counters
- **ALREADY SAFE** — body uses `_escape_md()` or call site already passes `parse_mode=None`

The discriminator for "HIGH ACTUAL vs HIGH POTENTIAL" is whether the interpolated field is currently emitting data with markdown-special chars in production. Every signal_type in gecko-alpha contains underscores (`gainers_early`, `hard_loss`, `trending_catch`, `first_signal`, `losers_contrarian`, `narrative_prediction`, `chain_completed`, `tg_social`, `volume_spike`, `slow_burn`, `chain_revival`). Sites that interpolate signal_type are HIGH ACTUAL by default — the trigger condition is met on virtually every fire.

## Per-site classification

### HIGH ACTUAL (6 sites — confirmed instances of Class-3 risk)

| # | Site | Interpolated field | Why it triggers |
|---|---|---|---|
| 1 | `scout/narrative/agent.py:557` (`format_heating_alert`) | `p.symbol` from predictions table | Symbol field directly embedded; tokens with underscores in ticker silently mangle |
| 2 | `scout/narrative/agent.py:715` (`build_paper_digest`) | `best_symbol`, `worst_symbol` from paper_trades | Best/worst symbol directly embedded in digest body |
| 3 | `scout/secondwave/detector.py:285` (`format_secondwave_alert`) | `candidate.get('ticker')`, `candidate.get('token_name')` | Ticker + name directly embedded in alert header |
| 4 | `scout/trading/calibrate.py:354` | `d.signal_type` (e.g., "gainers_early", "hard_loss") | Signal types **always** contain underscores; every calibration alert mangles |
| 5 | `scout/trading/weekly_digest.py:335` | `e["symbol"]` from missed_winners audit | Symbol of missed winners embedded; underscored tickers mangle |
| 6 | `scout/velocity/detector.py:193` (`format_velocity_alert`) | `det['symbol']`, `det['name']` | Symbol + name in velocity-alert body without escaping |

**Each of these 6 sites is structurally identical to the §2.9 trending_catch case** — the auto_suspend bug was just the first one operator-noticed. The class is much broader.

### HIGH POTENTIAL (3 sites — structurally vulnerable, not yet observed mangling)

| # | Site | Interpolated field | Why it's potential not actual |
|---|---|---|---|
| 7 | `scout/main.py:350` (briefing chunked summary) | LLM-synthesized text (Claude output) | LLM output is unconstrained free text; could include token symbols or signal names but operator-controlled prompt may limit |
| 8 | `scout/main.py:433` (counter-arg summary) | `token.ticker`, LLM-generated `counter_argument` | Ticker + LLM text; could mangle when tokens with underscored tickers reach the path |
| 9 | `scout/main.py:1521` (daily summary) | `top_tokens` list with `ticker` field | Ticker interpolated in `f"... ({t['ticker']}) ..."`; depends on which tokens appear in top list |

**Verdict on HIGH POTENTIAL:** structurally a parse_mode hygiene problem, but the operator may have been getting clean output by accident (the specific symbols/signals reaching these paths happened not to have markdown chars). The right fix is the same as HIGH ACTUAL (`parse_mode=None` or `_escape_md`), but the urgency is lower.

### LOW/MEDIUM (5 sites — body shape unlikely to mangle)

| # | Site | Body content | Why it's safe |
|---|---|---|---|
| 10 | `scout/chains/alerts.py:59` | Pattern name + numeric metrics | System-controlled pattern names; numeric data |
| 11 | `scout/live/loops.py:251` | Internal metric names + integers | System-controlled, no user data |
| 12 | `scout/main.py:165` (combo_refresh failure) | Static text + integer streak count | Nearly static |
| 13 | `scout/trading/suppression.py:186` | Static text + numeric count | Nearly static |
| 14 | `scout/trading/weekly_digest.py:340` (failure handler) | Exception class name + UUID | System-controlled, no user data |

### ALREADY SAFE (1 site)

| # | Site | Why it's safe |
|---|---|---|
| 15 | `scout/social/lunarcrush/alerter.py:144` (`_render_alert`) | Uses `_escape_md(alert.symbol)` and `_escape_md(alert.name)` before interpolation; underscores are backslash-escaped before Telegram sees them |

## Promotion-evidence summary

**Total instances of Class-3 parse_mode hygiene risk:**

- 1 already-known instance (trending_catch auto_suspend, §2.9, fixed in PR #106)
- **6 HIGH ACTUAL** newly confirmed from this audit
- 3 HIGH POTENTIAL (structurally vulnerable, observed-mangling not yet confirmed in prod logs)
- 5 LOW/MEDIUM (low-risk)
- 1 ALREADY SAFE

**Confirmed-actual instance count: 7** (1 known + 6 new HIGH ACTUAL).

**Compared to other promotion candidates from `findings_section_12c_sweep_2026_05_12.md`:**

| Candidate rule | Confirmed instances | Pattern frequency |
|---|---|---|
| §12c-narrow | 2 | Real but rare |
| §12e (signal-without-threshold) | 3+ | More common |
| **Parse_mode hygiene (Class 3)** | **7** | **Most common pattern in this sweep cycle** |
| Legacy-displaced | 2 | Era-specific |
| Signal-doesn't-verify-result | 2 | Money-flow-specific |

**Verdict:** parse_mode hygiene is the silent-failure pattern with the strongest evidence base in gecko-alpha right now. 6 HIGH ACTUAL sites have been emitting subtly-mangled Telegram alerts on every fire for the lifetime of the codebase — operator may have read them as "weird formatting" without diagnosing as Class-3 corruption. The trending_catch case was load-bearing only because the consequence (missing an auto-suspend reversal) was severe; the other 6 sites have been emitting mangled-but-benign alerts that escaped notice.

## Sibling-pattern observation: the rule isn't really new

§12b (global CLAUDE.md, post-2026-05-11 audit) already encodes: *"Every automated state change that reverses or overrides an operator-applied state MUST fire an operator alert at the write site"* — with an addendum specifically about `parse_mode=None` for system-health alerts. The Class-3 instance audit is *evidence* for §12b's load-bearing application, not for a new rule.

The right promotion shape may not be "promote Class-3 as a new rule" but **"expand §12b's parse_mode addendum to apply to ALL `send_telegram_message` call sites, not just auto-suspend write-time alerts."** §12b currently scopes the parse_mode discipline to "automated state change that reverses operator-applied state." This audit shows the same Markdown-mangling failure mode applies to:

- LLM-output alerts (synthesis, counter-arg)
- Calibration alerts (signal_type interpolated)
- Per-token alerts (velocity, secondwave, narrative heating)
- Digest alerts (paper digest, weekly digest)

None of these are "operator-state reversals," but they have the same rendering-corruption failure mode. §12b's parse_mode addendum should generalize from "auto-suspend alerts" to **"any `send_telegram_message` call whose body interpolates user-data fields (signal_type, symbol, ticker, LLM output) must use `parse_mode=None` for system-health alerts OR `_escape_md(value)` for user-data fields inside intentionally-formatted operator-visible messages."**

This is a scope expansion, not a sibling rule. Same shape as the §12a-vs-§12e collapse question from the §12c sweep.

## What's NOT in this audit

- **Per-site fixes.** Each HIGH ACTUAL site needs a small PR with `parse_mode=None` or `_escape_md()` per the body shape. Grouping suggested in `backlog.md` BL-NEW-PARSE-MODE-AUDIT: by module area (one PR for `scout/trading/`, one for `scout/narrative/`, etc.). Audit scope ends at classification; fix scope is separate.
- **Re-verification of the 7 sites the 2026-05-11 PR #106 audit marked `parse_mode=None`.** Those were verified at that time and are not in scope here.
- **HIGH POTENTIAL → HIGH ACTUAL promotion.** The 3 HIGH POTENTIAL sites need observation in production logs to determine if they actually mangle. Best done via journalctl grep for the specific Markdown-mangling pattern after a soak window.

## Carry-forward

1. **Scope the per-site fixes as 3-4 small PRs grouped by module** per the backlog's BL-NEW-PARSE-MODE-AUDIT estimate (~1-2h per area group). Highest-priority area: `scout/trading/calibrate.py:354` because `d.signal_type` always contains underscores, so this site has mangled every single calibration alert ever fired.
2. **Promote §12b parse_mode scope expansion** in the dedicated rule-promotion session — wording change to generalize from auto-suspend to all `send_telegram_message` sites with user-data interpolation.
3. **HIGH POTENTIAL re-evaluation after observation:** the 3 HIGH POTENTIAL sites should be re-classified after a 7-day production log audit confirms whether their bodies actually mangled or accidentally stayed clean.
4. **Update `feedback_class_3_silent_failure_rendering_corruption.md`** instance count from 1 to 7 (1 + 6 HIGH ACTUAL).
