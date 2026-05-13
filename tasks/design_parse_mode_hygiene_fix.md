**New primitives introduced:** NONE — applies existing `parse_mode=None` and `_escape_md` primitives from `scout/alerter.py`. This design doc captures cross-cutting decisions (the per-site decision tree, test architecture, audit-methodology fix) that are not in the per-task plan.

# Parse-Mode Hygiene Fix — Design

Companion to `tasks/plan_parse_mode_hygiene_fix.md`. Plan covers step-by-step procedure; this doc covers **why** the chosen shape is right and what cross-cutting decisions it implies.

---

## 1. Problem framing

Telegram's MarkdownV1 parser (`parse_mode="Markdown"`, the default in `scout.alerter.send_telegram_message`) silently consumes `_`, `*`, `[`, `]`, and backtick as formatting markers. When a message body interpolates user-data fields containing those characters, Telegram returns **HTTP 200 with the message rendered corrupted** — Class 3 silent failure (delivered but wrong).

Three properties of gecko-alpha make this systemic:
- Every `signal_type` value contains an underscore (`gainers_early`, `hard_loss`, `trending_catch`, etc.).
- Token symbols/tickers occasionally contain underscores or other markdown-special chars (e.g., `AS_ROID`).
- LLM-generated content (narrative summaries, counter-arguments) is unconstrained and can contain any character.

The `trending_catch` auto-suspend bug at §2.9 (`scout/trading/auto_suspend.py:254`, fixed PR #79 2026-05-06) was the first operator-noticed instance. Audit at `tasks/findings_parse_mode_audit_2026_05_12.md` showed it was not unique — 6 additional HIGH ACTUAL sites exist. Plan-review (this PR) discovered a 7th: `scout/alerter.py:189 send_alert`, the primary candidate-alert path, with hardcoded `parse_mode=Markdown`.

## 2. Solution shape: per-site choice between two existing primitives

Two primitives already exist in `scout/alerter.py`:

- **`parse_mode=None`** — disables MarkdownV1 parsing entirely. Body is treated as plain text. Telegram still auto-links bare URLs (clients do client-side URL detection), but `*bold*`, `[label](url)`, `` `code` `` are rendered as literal characters.
- **`_escape_md(value)`** — prepends `\` to each occurrence of `\ _ * [ ] \`` in a single field value. Caller still passes `parse_mode="Markdown"`, but the escaped field is protected.

### Decision tree

For each call site, the choice depends on **whether the message body's author intentionally uses Markdown formatting**:

```
Does the body use *bold*, _italic_, [label](url), `code`, or ``` code block ```?
│
├── NO (plain-text intent) ──► use parse_mode=None at the dispatch call
│                              (cheaper: 1 kwarg added; covers every interpolated field;
│                               immune to truncation-induced 400s)
│
└── YES (Markdown intent) ──► use _escape_md(value) on each user-data interpolation
                               (keeps bold/links working; needs per-field touch)
```

### Applied per site (matches plan's per-site matrix)

| # | Site | Author intent | Primitive |
|---|---|---|---|
| 1 | narrative heating alert | plain text | `parse_mode=None` |
| 2 | paper trading digest | plain text | `parse_mode=None` |
| 3 | secondwave alert | plain text | `parse_mode=None` |
| 4 | calibrate apply alert | plain text (matches dry-run already) | `parse_mode=None` |
| 5 | weekly digest (2 sites) | plain text | `parse_mode=None` |
| 6 | velocity alert | Markdown (`*bold*` + `[chart](url)`) | `_escape_md` |
| 7 | send_alert / format_alert_message | Markdown (`*{token_name}*` bold) | `_escape_md` |

### Why not a single global fix (e.g., flip the default)?

Considered and rejected:
- **(a) Flip `send_telegram_message` default to `parse_mode=None`.** Breaks the ~7 already-fixed call sites that pass nothing and currently get Markdown rendering through documented intent. Migration burden equals this PR plus an audit of every remaining caller. Net cost: same per-site work without the per-site clarity.
- **(b) Auto-escape inside `send_telegram_message`.** Would double-escape any caller that already escapes (e.g., `lunarcrush/alerter.py:144`). Would mangle intentional Markdown formatting (e.g., velocity's bold) by escaping the formatter chars themselves. Wrong layer for the fix.
- **(c) New wrapper function (`send_safe_telegram_message`).** Adds a primitive without solving the problem — callers still have to choose between Markdown-with-escape and plain. Spec proliferation without value.

**Per-site explicit choice is right** because the intent (Markdown formatting vs plain text) is a per-message decision the author already made. Making it explicit at the dispatch is documentation, not noise.

## 3. Out-of-scope decisions

### 3a. `scout/main.py:434` (counter-arg) deferral

Plan-review reviewer A argued for promotion to HIGH ACTUAL based on `[HIGH]`/`[CRITICAL]` brackets being mis-parsed as Markdown link anchors. **Verified incorrect**: Telegram MarkdownV1 link syntax requires `[label](url)` adjacency. The body at line 425 is `f"- [{f.severity.upper()}] {f.flag}: {f.detail}"` — bracket followed by whitespace + text, not `(`. Bare `[HIGH]` renders as literal text. The underlying ticker + LLM-output Markdown-char concern is real but matches the audit's HIGH POTENTIAL classification — defer for the audit's stated reason (need 7-day production observation before promoting).

### 3b. HIGH POTENTIAL sites in `scout/main.py`

Three sites (`:350`, `:434`, `:1521`/`:1537`). All structurally vulnerable. None promoted in this PR. Carry-forward: log audit after a 7-day soak post-deploy; promote any site that's observed to mangle.

### 3c. Audit-methodology gap (THIS PR's discovery)

The audit grepped `send_telegram_message` only. It missed `send_alert` at `scout/alerter.py:189` — a sibling function that does its own `session.post(...sendMessage...)` with hardcoded `parse_mode=Markdown`. **The audit's grep should have been `(send_telegram_message|sendMessage)`** to catch all Telegram-send paths. The plan adds a structural Layer 3 AST test that catches the `send_telegram_message`-call case mechanically; the `session.post(.../sendMessage)`-call case is one-known-site (now fixed) and re-audit is filed as a deferred follow-up.

### 3d. Docstring contracts (design-review fold)

Per design-review reviewer D: hidden coupling risk on `format_velocity_alert` and `format_alert_message` — the convention "caller passes raw fields, formatter escapes" is implicit. The next formatter author may forget (the audit just demonstrated callers forget for the lifetime of the codebase). Mitigation: add a one-line docstring contract on each escaping formatter stating the rule and pointing at CLAUDE.md §12b. Zero LOC in callers; ~2 lines per formatter. Discoverability defense.

Reviewer D explicitly evaluated and rejected: new wrapper functions (`send_plain_telegram_message`, `send_safe_telegram_message`), vararg `_escape_fields` helper, template-DSL `format_safe_alert_body`, and type-system aids (`NewType("MarkdownSafeStr", str)`). Codebase already has 7+ raw-primitive sites (e.g., `auto_suspend.py:266,322`, `tg_alert_dispatch.py:311`, `lunarcrush/alerter.py:144`); adding a new pattern would either cause migration scope creep or two-pattern coexistence — both worse for future maintainers than staying with raw primitives.

## 4. Test architecture

Three layers of test coverage. Each layer catches a different failure mode.

### Layer 1: Formatter render assertion (Tasks 7, 8)

For sites that use `_escape_md` (velocity #6, alerter #7), assert that the **rendered output** contains the escaped form of user-data fields. Example:

```python
text = format_velocity_alert([{"symbol": "AS_ROID", ...}])
assert "AS\\_ROID" in text  # literal chars: A, S, \, _, R, O, I, D
assert "*AS\\_ROID*" in text  # intentional Markdown wrapper preserved
```

**Catches:** future edits to the formatter that remove the `_escape_md` call.
**Misses:** changes to the dispatch call (parse_mode flip).

### Layer 2: Call-site source-level pin (Tasks 2-6)

For sites that use `parse_mode=None` (sites #1-5), assert that the dispatch call site has the kwarg in the source. Example:

```python
source = inspect.getsource(scout.narrative.agent)
idx = source.index("format_heating_alert(")
tail = source[idx : idx + 600]
assert "send_telegram_message(" in tail
assert "parse_mode=None" in tail
```

**Catches:** future refactors that remove the kwarg or restructure the dispatch.
**Misses:** silent regressions where a new dispatch path is added without the kwarg (the test only inspects the one paragraph around the known formatter call).

### Layer 3 (PROMOTED from REJECTED — design-review fold): AST-based structural test

Original disposition was REJECTED — rationale was that pattern-matching dispatch calls in source is fragile (multi-line function calls, kwargs from variables, etc.). Design-review reviewer C correctly identified that the rejection rationale was wrong: AST-based walking handles all the cited fragility cases natively. `ast.walk` over `ast.Call` nodes finds every `send_telegram_message` invocation regardless of formatting, multi-line layout, or kwarg-from-variable; then the test asserts `parse_mode` is in `[kw.arg for kw in call.keywords]`.

The audit's missed `send_alert` is the empirical case for keeping this test:
- The original audit grepped `send_telegram_message` source-occurrences and missed `send_alert` because it does its own `session.post(...sendMessage...)` (not a `send_telegram_message` call).
- An AST test pinned at PR time would catch a new dispatch site added 6 months from now without `parse_mode=`, even before an operator notices the mangled alert.

Implemented as `tests/test_parse_mode_hygiene.py::test_all_dispatch_sites_pin_parse_mode` (Task 1.1). Caveat: it only catches sites that go through `send_telegram_message`. Sites that do their own `session.post(...sendMessage...)` (like the pre-fix `send_alert`) are not caught — for those, the AST walker would need a second arm matching `session.post` with a URL containing `/sendMessage`. Deferred (one known site; mark as a follow-up if a future audit finds more).

### Layer 4 (added — design-review fold): wire-level integration tests

Per design-review reviewer C: source-level pins (Layer 2) miss wire-level regressions. The fix is an `aioresponses`-mocked test per primitive that captures the actual JSON payload posted to Telegram. Two tests:
- `parse_mode=None` path → asserts `parse_mode` key is ABSENT from payload (per `scout/alerter.py:143-144` conditional include).
- `parse_mode='Markdown'` + `_escape_md` path → asserts payload `text` contains the escaped form AND `parse_mode == "Markdown"`.

Implemented in Task 8.5.

### URL-path no-escape pins (added — design-review fold)

For sites #6 (velocity) and #7 (alerter), URL path fields (`coin_id`, `contract_address`) sit inside `[label](url)` link targets where Telegram requires literal characters. The original tests asserted escape on data fields but did not pin the no-escape decision on URL fields. A future "helpful" PR that escapes those fields would break every chart link / CoinGecko link silently. Added two pin tests:
- `test_velocity_alert_url_path_not_escaped` — `coin_id="asteroid_coin"` round-trips literal underscore through URL
- `test_format_alert_message_url_path_not_escaped` — `contract_address="0xabc_def"` round-trips through both DexScreener and CoinGecko URL paths

### Why no end-to-end test against real Telegram?

Telegram's render output is server-side; the only way to verify rendering is visual inspection of the delivered message. The fix is structural (parse_mode kwarg / escape application), and the structural tests pin that. Post-deploy verification is operator-eyeball on the next alert fire per path (covered in the plan's deploy plan).

## 5. Rollback semantics

Each task is a single self-contained commit. Per-site rollback is `git revert <sha>` — no cascading dependencies between sites. Task ordering in the plan is independent (any order works) but follows audit-doc number order for traceability.

Specifically:
- Site #4 (calibrate apply) is the most operator-visible fix — every calibration alert has been mangling. Rollback would re-introduce mangling, not crash anything.
- Site #7 (send_alert) touches the highest-volume path. Rollback would re-introduce mangling on every gate-pass alert.
- Site #6 (velocity) requires both formatter fix and test update; rollback must include both.

No site fix changes message wire format in a way that would crash a downstream consumer. Telegram accepts both Markdown and plain text on the same chat. Operator client renders both fine.

## 6. Deploy + verification telemetry

**Pre-deploy:** all tests pass locally, branch pushed, PR reviewed.

**Deploy:** per plan's deploy plan — `git pull` + pycache clear + service restart on srilu VPS.

**Verification (per-site, on next alert fire):**

| Site | Fire frequency | Verification |
|---|---|---|
| #1 narrative heating | continuous | next heating alert; check `_` chars render literally |
| #2 paper digest | daily (UTC midnight) | next day's digest |
| #3 secondwave | continuous when narrative+secondwave detect | next fire |
| #4 calibrate apply | weekly Mon 02:00 UTC | next Monday's calibration |
| #5 weekly digest | weekly Mon ~03:00 UTC | next Monday's digest |
| #6 velocity | continuous | next velocity alert |
| #7 send_alert | every gate-pass token | next candidate alert |

**What to look for:** signal names (`gainers_early`, etc.) render with underscores visible (not consumed as italics). Token symbols/names render literally. No "weird formatting" complaints.

**Negative verification:** if any of the 7 paths fires post-deploy and STILL mangles, that's a deploy issue (pycache not cleared, service not restarted, wrong branch deployed). Check `git log` on VPS, `pycache` state, service restart timestamp.

## 7. Hermes-first analysis (carry-forward from plan)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram parse_mode hygiene | none found | In-tree `_escape_md` + `parse_mode=None`; pattern already deployed at 5+ sites |
| Markdown-injection escaping | none found | Same |

Verdict: no Hermes ecosystem opportunity here. In-tree primitives are the canonical fix.

## 8. Cost analysis

- **Plan + design doc:** ~3 hours of session time (this work).
- **Implementation:** 8 commits × ~10 minutes per commit = ~80 minutes.
- **PR review:** 3 parallel reviewers + fold + push = ~2 hours.
- **Total estimated session time:** ~6 hours.
- **Operator time post-merge:** ~10 minutes (deploy + verify next-fire on each path).

**Value delivered:**
- 7 silent-failure sites closed.
- Class-3 evidence base for §12b scope expansion (next rule-promotion session).
- Audit-methodology gap surfaced; broadens future audits.

**Cost asymmetry:** ~6 hours of session work vs every alert from these 7 paths having been silently mangling since the codebase's inception. Net positive even if operator never visually noticed the mangling (which they did — §2.9 was operator-noticed and load-bearing for the auto-suspend signal-state mental model).
