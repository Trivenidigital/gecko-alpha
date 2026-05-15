# Lessons

## X Alerts asset links must cover unresolved cashtags

- 2026-05-15 correction: I treated "clickable Asset column" as only safe for
  confidently resolved assets. That left the common V1 X-alert case
  (unresolved cashtag-only rows like `$GIGA`, `$PRIMIS`, `$NOTHING`) unlinked
  even though the user expected every visible asset chip to be clickable.
- Rule: when adding dashboard links for signal assets, provide a conservative
  search fallback for unresolved display identifiers. Use exact market pages
  when the backend has a confident `resolved_coin_id` or contract+chain; use a
  search page when confidence is lower. Do not silently render the primary
  visible asset as inert unless there is no identifier at all.

## Hermes-first applies to existing custom-code debt, not only future diffs

- When the operator says the project is bearing too much custom code despite Hermes-first pressure, do not only strengthen future plan templates. Run a backlog/shipped-module comparison against current installed VPS skills/plugins and the public Hermes/agent-skills ecosystem. Classify existing items as `KEEP_CUSTOM`, `USE_SKILL_AS_REFERENCE`, `REPLACE_WITH_HERMES`, `BRIDGE_TO_HERMES`, or `DELETE_OR_DEFER`, then update the backlog so stale "none found" conclusions do not keep driving new custom work.
- The debt audit should preserve project-owned runtime boundaries. A skill can replace workflow intelligence or serve as API-reference without replacing gecko-alpha's durable DB writes, scoring, watchdogs, dashboards, or operator audit trail.

## Hermes-first review scope

- When reviewing proposed custom code, "Hermes capabilities" means more than the public Hermes skill hub. Check the deployed VPS Hermes surface first: installed skills, plugins, and relevant `.hermes` artifacts on the VPS. Also include upstream ecosystem checks such as `NousResearch/hermes-agent-self-evolution` and `0xNyk/awesome-hermes-agent` before accepting a custom implementation as justified.

## 2026-05-13 -- Parse-mode hygiene Class-3 fixes (BL-NEW-PARSE-MODE-AUDIT)

**Lesson:** Default `parse_mode="Markdown"` in `scout.alerter.send_telegram_message`
is a footgun for any caller whose body interpolates user-data fields with
underscores. The trending_catch auto-suspend bug (CLAUDE.md §2.9, fixed in PR #106)
was not unique -- 7 HIGH ACTUAL sites had been silently emitting mangled
alerts for the codebase's lifetime (6 from audit + 1 plan-review discovery).

**Audit-methodology lesson:** the original audit grepped `send_telegram_message`
only and missed `send_alert` at `scout/alerter.py:189` -- a sibling function that
does its own `session.post(.../sendMessage)` call. Future Telegram-send audits
must grep both patterns: `(send_telegram_message|sendMessage)`. The AST-based
structural test added in this PR (`test_all_dispatch_sites_pin_parse_mode`)
closes the recurrence gap for the `send_telegram_message` arm.

**Rule (extension of CLAUDE.md §12b parse_mode addendum):** Any Telegram-send
call whose body interpolates `signal_type`, `symbol`, `ticker`, LLM-generated
text, or any other field that may contain `_ * [ ] \`` must either:
1. Pass `parse_mode=None` (preferred for system-health / digest / plain-text
   alerts), OR
2. Wrap each user-data field with `_escape_md()` AND keep `parse_mode="Markdown"`
   (only when the message intentionally uses Markdown formatters like `*bold*`
   or `[link](url)`).

**Default:** when in doubt, choose `parse_mode=None`. Markdown is only worth
keeping when the formatting is operator-visible and load-bearing (e.g., velocity
alerts with clickable chart links, primary token alerts with bold token names).

**Windows line-ending tooling lesson:** the Edit tool converts LF to CRLF when
rewriting Python source files on Windows. A line-ending-normalization pass plus
`Path.write_bytes` via a helper script preserved LF cleanly. For future
multi-file edits on Windows, prefer byte-level Python helpers over the Edit tool
when the file is being modified (not created fresh).
