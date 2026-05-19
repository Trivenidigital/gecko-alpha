**New primitives introduced:** NONE. This is a plan/design doc only. It scopes a new watchdog shell script + cron schedule + hysteresis state directory; no code, no schema migration, no behavior change ships in this PR. The "primitives" listed below (`revival-verdict-watchdog.sh`, cron entry, state dir) are *proposed* — they ship when the implementation PR opens, gated on the actionability runbook review and operator approval.

# Plan: BL-NEW-REVIVAL-VERDICT-WATCHDOG — 2026-05-19

## Guardrail

Plan/design only. The 24h actionability validation window is still
accumulating (cutover 2026-05-19T11:39:09Z, now 2026-05-19T16:02:55Z,
~19h remaining). No suppression, no capital-allocation change, no entry-
rule change, no gate-threshold change.

This watchdog itself, when implemented, is **non-suppressing**: it emits a
Telegram operator alert, it does NOT auto-revoke verdicts or change trade
state.

## Problem Statement

PR #150 (`BL-NEW-LC-REVIVAL-CRITERIA-TIGHTENING`, merged a20891f
2026-05-17T21:48:57Z) ships verdict-stamp machinery that writes audit rows
of the form:

```
signal_params_audit.field_name = 'soak_verdict'
signal_params_audit.new_value  = 'keep_on_provisional_until_<ISO-8601>'
```

…where `<ISO-8601>` is the verdict's structural expiry (30d default per
`Settings.REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS`). The expiry exists to
force re-evaluation: a verdict's "PASS" rests on a soak-window cohort that
becomes stale.

The current operating model is: operator runs the evaluator manually
before the verdict expires; if the verdict still holds on fresh data, the
operator emits a new audit row resetting the clock.

The risk: **if the operator forgets to re-run, the audit row sits as a
stale "valid" verdict indefinitely.** No primitive fires; the system
behaves as if the verdict is still load-bearing.

Per CLAUDE.md §12-style silent-non-failure rule:

> if it looks like a primitive but doesn't fire, the operator's mental
> model is wrong about the system.

The watchdog closes that gap.

## Drift Check (CLAUDE.md §7a)

Per-step tree-grep against latest `origin/master` (32df89d).

### Backlog entry

`backlog.md:1835` — `BL-NEW-REVIVAL-VERDICT-WATCHDOG: active enforcement
of keep_on_provisional_until_<iso> expiry`. Status PROPOSED 2026-05-17.
Recommends approach (a): "emit operator alert 'verdict expired, re-run
evaluator'" over (b) auto-write-revoke. Operator-decision-point preserved.

### Verdict-stamping mechanism (already shipped)

- `scout/trading/revival_criteria.py:698-748` — `_emit_soak_verdict_sql`
  builds the `keep_on_provisional_until_<iso>` row. Microseconds are
  truncated to keep the watchdog's parse-back robust (PR-stage reviewer
  #3 finding #4).
- `scout/db.py:1739-1753` — `signal_params_audit(id, signal_type,
  field_name, old_value, new_value, reason, applied_by, applied_at)` plus
  index `idx_signal_params_audit_signal_at(signal_type, applied_at)`.
- `scout/trading/revival_criteria.py:357-365` — helper already exists for
  "fetch most-recent soak_verdict audit row per signal_type." Watchdog
  can either reuse this helper or re-implement the SQL inline (shell
  script context favors inline SQL).

### Existing watchdog primitives (modeling pattern)

`scripts/` contains six watchdog shell scripts that establish the pattern:

| Script | Purpose | Patterns it establishes |
|---|---|---|
| `held-position-price-watchdog.sh` | Alert if any open paper_trade has stale price_cache | Hysteresis state dir, curl-direct TG, plain-text alerts, exit-code convention |
| `cron-drift-watchdog.sh` | Alert on cron-fragment drift | `.env` parsing tolerance |
| `chain-anchor-health-watchdog.sh` | Chain pipeline freshness | Similar |
| `minara-emission-persistence-watchdog.sh` | Minara emission table freshness | §12a freshness SLO style |
| `gecko-backup-watchdog.sh` | Backup rotation health | Curl-direct TG convention (the canonical citation) |
| `systemd-drift-watchdog.sh` | systemd unit drift | Per-unit-name parsing |

**Best fit to model on: `held-position-price-watchdog.sh`** — it has the
closest shape (SQLite query → count threshold → hysteresis → curl-direct
TG alert with worst-offender detail).

### Production state today

Queried `signal_params_audit` on srilu-vps `/root/gecko-alpha/scout.db`
on 2026-05-19T16:02:55Z (after PR #150 merged 2026-05-17):

| signal_type | field_name | new_value | applied_at |
|---|---|---|---|
| losers_contrarian | soak_verdict | `keep_on_permanent` | 2026-05-13T04:05:02Z |
| gainers_early | soak_verdict | `keep_on_permanent` | 2026-05-13T04:05:02Z |
| __hpf__ | soak_verdict | `dry_run_continued` | 2026-05-13T04:05:02Z |

```
COUNT(*) WHERE new_value LIKE 'keep_on_provisional_until_%' = 0
```

**Zero `keep_on_provisional_until_*` rows exist on prod today.** The
watchdog's first job at deploy time is to handle the empty-input case
gracefully (clean exit 0, no false-positive). This matters because
implementing the watchdog before the first provisional verdict exists is
correct order — we want the watchdog live *before* operator emits a
provisional row, so the freshness invariant is not retroactively broken.

### Residual gap

- No watchdog primitive monitors `signal_params_audit` rows for expiry.
- The legacy `keep_on_permanent` value has no expiry semantics by design
  (operator's explicit permanent stamp); the watchdog must ignore those.
- `dry_run_continued` is similarly a non-expiring procedural verdict;
  ignore.
- Only `keep_on_provisional_until_<iso>` carries an embedded expiry that
  the watchdog acts on.

## Hermes-first Analysis (CLAUDE.md §7b)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Time-boxed verdict expiry watchdog | None (Hermes skill hub returned no matches; 691 skills surveyed). | Build in-project; this is a DB-row-driven, project-local scheduled job. |
| Cron-driven DB-query alerting | None matching this shape. Hermes "webhook-subscriptions" is event-driven, not poll-DB. | Build in-project. |
| Stale-decision detection in trading systems | None. | Build in-project. |

`awesome-hermes-agent` ecosystem check: 404 (consistent with prior
sessions). Verdict: no Hermes-side primitive applies; the work is a
gecko-alpha-local shell script + cron entry + state dir, modeled on the
existing `scripts/*-watchdog.sh` pattern.

## Scope

### In-scope

1. New shell script `scripts/revival-verdict-watchdog.sh`.
2. New cron fragment (under the new `cron/` bracketed pattern shipped in
   cycle 11) installing daily execution.
3. New state directory `/var/lib/gecko-alpha/revival-verdict-watchdog/`
   for hysteresis counter + last-alert-per-signal idempotency.
4. Test coverage: a small test that drives the script against a fixture
   `signal_params_audit` table.
5. Runbook for operator: how to read the alert, how to clear hysteresis.

### Out-of-scope (explicit non-goals)

- **No auto-revoke.** Approach (b) from the backlog ("auto-write a revoke
  row") is explicitly rejected; would be a §12b case (automated state
  reversal of operator-applied state) and would damage the
  "operator-decision-point preserved" property.
- **No suppression / capital-allocation / live-trading change.**
- **No schema migration.** The watchdog reads `signal_params_audit`,
  which already exists.
- **No new metric tables.** Per-fire telemetry goes to structured logs
  (`revival_verdict_watchdog_*`); if a persistent freshness-monitoring
  surface is wanted, it ships as a follow-up against
  `BL-NEW-EVALUATION-HISTORY-PERSISTENCE`.
- **No per-signal threshold tuning.** The expiry days come from
  `Settings.REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS`, which is set by the
  evaluator at verdict-emission time. The watchdog parses the embedded
  ISO timestamp; it does NOT independently compute "30 days from
  applied_at."
- **No retrospective alerting.** At deploy time, if any expired
  provisional row exists, the watchdog should NOT spam-alert. First-run
  behavior must be designed (see Mechanism §4).

## Mechanism

### Inputs

- DB: `/root/gecko-alpha/scout.db`
- Query: most-recent `signal_params_audit` row per `signal_type` where
  `field_name = 'soak_verdict'`:

  ```sql
  WITH latest AS (
    SELECT signal_type,
           MAX(applied_at) AS max_at
    FROM signal_params_audit
    WHERE field_name = 'soak_verdict'
    GROUP BY signal_type
  )
  SELECT a.signal_type, a.new_value, a.applied_at
  FROM signal_params_audit a
  JOIN latest l
    ON l.signal_type = a.signal_type
   AND l.max_at      = a.applied_at
  WHERE a.field_name = 'soak_verdict';
  ```

- For each row, parse:
  - if `new_value` does NOT start with `keep_on_provisional_until_`,
    skip (legacy `keep_on_permanent`, `dry_run_continued`, etc.).
  - else extract the ISO timestamp after the prefix and compare to `now`.

### Decision logic

```
for each signal_type:
  if latest_verdict starts with 'keep_on_provisional_until_<iso>':
    if parsed iso < now:
      mark signal_type as expired
```

### Hysteresis

Daily cron fires once/day. A single expired row is the actual condition —
no transient blip risk. **Hysteresis is not the daily check.** Instead,
**per-signal alert idempotency** prevents spamming.

- State dir: `/var/lib/gecko-alpha/revival-verdict-watchdog/`
- Per-signal file:
  `last_alert_<signal_type>` containing the ISO timestamp of the last
  alert sent for that signal_type's expiry event.
- If the same `signal_type` is already in expired state and the last
  alert was sent within `REVIVAL_VERDICT_WATCHDOG_REALERT_HOURS`
  (default 168, i.e., weekly), do NOT re-alert. Otherwise alert and
  update the state file.
- When the operator emits a fresh verdict (a new `signal_params_audit`
  row with `applied_at > last_alert_<signal_type>`), the state file is
  no longer load-bearing for that signal; next expiry event will alert
  immediately.

This avoids spam (re-alert at most weekly per signal) while preserving
the §12b "must alert at write time" property — except this is not a
write event, it's a *non-event* (a verdict that should have been
renewed but wasn't). The §12-style applicable rule is §12c-narrow
(health-claim-vs-output-truth-for-specific-subset) and the §12-narrow
candidate of "signal-without-actionable-threshold" — both are addressed
by the alert.

### First-run behavior

If the watchdog deploys and ANY provisional row is already expired at
first-run, send a single "first-run audit" alert summarizing all expired
signal_types. Subsequent runs follow the per-signal idempotency rule
above.

This prevents (a) silent miss of an existing expired row and (b)
multi-row spam at first run.

At deploy time today, 0 provisional rows exist; first-run will be a
clean no-op exit 0.

### Telegram delivery

- Direct `curl` POST to `https://api.telegram.org/bot<TOKEN>/sendMessage`
  (NOT `scout.alerter.send_telegram_message`, which swallows errors per
  `scripts/gecko-backup-watchdog.sh:11-13` documented choice).
- `parse_mode=None` — message body contains signal_type names with
  underscores (`losers_contrarian`, `gainers_early`); Markdown rendering
  would consume them per CLAUDE.md §12b Class-3 incident.
- Bot token + chat_id parsed from `/root/gecko-alpha/.env` with leading-
  whitespace tolerance (PR #161 pattern).

### Alert body shape (plain text)

```
⚠ revival-verdict-watchdog: <N> signal_type(s) have expired provisional verdicts.

EXPIRED:
- <signal_type>: verdict applied <APPLIED_AT>, expired <EXPIRED_AT> (<AGE> ago)
  reason: <REASON_TRUNCATED_120CHAR>

Action: re-run the revival-criteria evaluator. If verdict still PASSes,
emit a new audit row. If FAIL, follow runbook for the affected signal.

Runbook: tasks/runbook_revival_verdict_alert_response.md
```

### Exit codes

Modeled on `held-position-price-watchdog.sh`:

| Code | Meaning |
|---|---|
| 0 | No expired provisional verdicts, OR alert already sent within idempotency window |
| 1 | Alert delivered |
| 4 | DB not found / SQL error |
| 5 | TG token/chat_id missing or placeholder |
| 6 | python missing (JSON encoding) |
| 7 | Telegram HTTP delivery failed |

### Scheduling

- Daily once: `30 9 * * *` (09:30 UTC).
- Same cron file pattern as `cron/gecko-cron-fragment.sh` per cycle 11
  bracketed-fragment convention; idempotent deploy via
  `cron/deploy.sh`.
- Cron-fragment SENTINEL: `# BL-NEW-REVIVAL-VERDICT-WATCHDOG-START` /
  `# BL-NEW-REVIVAL-VERDICT-WATCHDOG-END` per established pattern.

## Failure Modes Pre-empted

Per CLAUDE.md §9 and §12 disciplines.

| Mode | How addressed |
|---|---|
| §9a — runtime-state verification before acting | Watchdog reads `signal_params_audit` directly each run; no cached state of verdict counts. Prod-state was verified at design time (0 provisional rows exist today). |
| §9c — post-hoc attribution discipline | Each alert names the specific `signal_type`, `applied_at`, and parsed expiry; operator can re-derive the chain. The reason field is truncated to 120 chars to keep the alert short while preserving cause attribution. |
| §12a — pipeline tables ship with freshness SLO + watchdog | This script IS the freshness SLO + watchdog for `signal_params_audit` rows of `field_name='soak_verdict'`. Not a row-rate watchdog (those rows fire on operator action, not continuously); it's a "row's-embedded-expiry-passed" watchdog. |
| §12b — automated state reversals of operator-applied state must alert | The watchdog does NOT reverse state. It alerts that the operator-emitted state has reached its declared expiry. The operator-decision-point is preserved (operator chooses to re-evaluate; watchdog doesn't write to the DB). |
| §12b — Class-3 silent rendering corruption | `parse_mode=None` on the Telegram call; signal_type underscores cannot be eaten. |
| Self-disabling failure: watchdog cron fragment silently removed | The cron-drift-watchdog from BL-NEW-CRON-DRIFT-WATCHDOG already catches drift on the cron file. No new primitive needed. |
| State-dir loss (`/var/lib/gecko-alpha/revival-verdict-watchdog/` wiped) | First-run behavior triggers: a single audit alert summarizes expired signals; not a regression. |
| Clock skew on srilu-vps | `date -u` is the source of "now"; SQLite `datetime('now')` is UTC. ISO timestamps in `new_value` are explicitly UTC per the emitter at `revival_criteria.py:722`. No timezone conversion in the watchdog. |
| Microsecond residue in the ISO timestamp | Emitter truncates microseconds at write time (`revival_criteria.py:719-721`). Watchdog parser must tolerate either with-microseconds or without. |

## Pre-registered Acceptance Criteria

These ship in the implementation PR's test plan; documenting now so the
implementation PR can be reviewed against them.

### Functional

1. With 0 `keep_on_provisional_until_*` rows in fixture DB → exit 0,
   no alert sent, structured log `revival_verdict_watchdog_run` with
   `expired_count=0`.
2. With 1 expired provisional row and clean state dir → alert sent,
   exit 1, last-alert state file written for that signal_type.
3. With 1 expired row and last-alert state file written within
   re-alert window → exit 0, no alert sent, log
   `revival_verdict_watchdog_realert_skipped`.
4. With 1 expired row and last-alert state file written outside
   re-alert window → alert sent, exit 1, state file updated.
5. With 1 row containing `keep_on_provisional_until_<iso>` whose ISO is
   in the future → exit 0, no alert.
6. With 1 row containing `keep_on_permanent` or `dry_run_continued` →
   exit 0, no alert (legacy verdicts ignored).
7. With malformed ISO timestamp in `new_value` → exit 4 (treat as
   schema/data corruption, not a watchdog false-negative), structured
   log identifies the offending row id.
8. With operator emitting a fresh `keep_on_provisional_until_<iso>` row
   after a previous alert → watchdog correctly recognizes the new row
   as the current verdict and resets idempotency for that signal_type
   on next expiry.

### Operational

9. Cron-deploy script (`cron/deploy.sh`) is idempotent: re-running
   does not duplicate the fragment.
10. Watchdog `journalctl` shows `revival_verdict_watchdog_dispatched`
    + `revival_verdict_watchdog_delivered` triplet on every alert
    (per CLAUDE.md §12b log-pair convention).
11. Telegram alert renders correctly when signal_type contains
    underscores (verify by deliberately emitting a fixture row for
    `losers_contrarian` and checking the rendered message).

### Non-regression

12. Watchdog reading `signal_params_audit` does not block evaluator
    writes (uses busy_timeout, never holds a write lock).
13. Existing watchdogs (`held-position-price-watchdog.sh`, etc.) are
    unaffected; their state dirs do not collide.

## Rollout Plan (proposed; implementation PR ships these)

1. **Pre-merge:** review this design doc; reviewer signs off on
   approach (a) over (b); operator confirms `REVIVAL_VERDICT_WATCHDOG_*`
   defaults.
2. **Implementation PR ships:** script + tests + cron fragment +
   runbook.
3. **Deploy to srilu-vps:** install cron via `cron/deploy.sh`. First
   run is clean no-op (0 provisional rows).
4. **First provisional-verdict event:** when operator runs the evaluator
   and emits a `keep_on_provisional_until_<iso>` row, watchdog tracks
   it.
5. **Soak:** at the 30d expiry, watchdog alerts. Operator re-runs
   evaluator. Operator confirms behavior matches design.
6. **Promote to load-bearing:** after one observed expiry-alert-cycle
   completes correctly, mark the watchdog as load-bearing in
   backlog.md.

## Backstop

If the watchdog implementation is not done by 4 weeks from PR #150 merge
(2026-06-14 per backlog), revisit at that calendar gate. The watchdog
itself is a convenience layer; the structural verdict-expiry property
ships with PR #150 (verdict carries embedded `<iso>` expiry). The
watchdog converts that latent property into an active operator
notification.

The "do nothing" failure mode is exactly what PR #150's design
acknowledged: operator-recall is the load-bearing safety net until the
watchdog ships. This plan moves that out of "operator-recall" into
"system-enforced."

## Open Questions

1. **Re-alert window default.** Proposed weekly (168h). Should this be
   shorter for high-priority signal_types? Defer to operator: if not
   raised, ship at 168h.
2. **Alert verbosity at first-run with multiple expired rows.** Proposed
   single summary alert. If operator prefers per-signal alerts at
   first-run, easy switch via env var. Defer to operator.
3. **State-dir migration.** If `/var/lib/gecko-alpha/` is ever moved,
   the per-signal alert idempotency files must move with it. Document
   in runbook.

## Decision Recommendation

After the 24h actionability validation runbook completes and is reviewed,
this design is safe to graduate to implementation independently of the
actionability outcome — the watchdog does not interact with the
actionability classifier in any way.

No runtime change should ship from this plan alone.
