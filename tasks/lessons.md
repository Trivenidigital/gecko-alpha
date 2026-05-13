# Lessons

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
