# Narrative-prediction lane — dormant structural debt and mandatory revival gate

**Date:** 2026-08-05
**Status:** **SUPERSEDED — HISTORICAL RECORD.** See the update block immediately
below. Nothing in this document was modified in production; it is retained as
forensic provenance, not as current runtime truth.
**Class:** dormant structural debt at time of writing. Zero behavioural impact
while quarantined.

---

## *** SUPERSEDED / POST-INVESTIGATION UPDATE — 2026-08-07 ***

**Everything below this block describes the system as of 2026-08-05 and must not
be read as current requirements.** A later investigation falsified the central
hypothesis and shipped code that removes most of the structural debt this
document was written to flag.

### The root-cause hypothesis is FALSIFIED

This document argues (§2, "HIGH confidence, NOT proven") that the learner failed
because `NARRATIVE_LEARN_MODEL = claude-sonnet-4-6` is a retired model ID, and
reasons that because `NARRATIVE_SCORING_MODEL` is current and an API key is
present, the fault is *path-specific, not credentials*.

**Both halves are wrong.** A read-only probe on 2026-08-07 using the production
key and `NARRATIVE_SCORING_MODEL` (`claude-haiku-4-5` — the model this document
calls "current") returned:

```
BadRequestError | 400 | BILLING_BLOCKED | req_011CdooRRpdEKNVoRZx8cSZm
"Your credit balance is too low to access the Anthropic API."
```

It **is** credentials — an exhausted account balance — and it is **not**
path-specific: the scoring path fails identically. The presence of an API key
was never evidence that billing was healthy. (This is the recorded
"billing ≠ retired model" trap, reproduced here in full.)

### What has since shipped

| Change | PR | Effect on this document |
|---|---|---|
| Deterministic, provider-free daily learner | **#510** | The required daily learning path no longer calls any provider. §2's failure mode cannot recur on that path. |
| Typed outcomes, secret-safe failure telemetry | #510 | Addresses G2 for the learner path. |
| Cadence anchor, shared bounds registry, scoped verdict | **#511** | Corrects the bounds/verdict handling §3 discusses. |
| Paper-only revival contract guard | **#513** | Revival can no longer silently re-enable Telegram alerting. |
| `score_token` diagnostics | **#514** | A scorer failure now reports provider health, status, request id and a bounded traceback — the §2.1 "traceback is discarded" defect, for the scoring path. |
| Dashboard lock/unlock route | #512-era | See below. |

### Specific claims that are no longer true

- **"Fixing the learner alone restores adaptation" — FALSE.** The daily path now
  runs `run_deterministic_daily_learn`, which is **proposal-only**: it writes a
  `learn_logs` provenance row and calls no `Strategy.set`. There is no automatic
  apply path, so no learner fix restores adaptation by itself. Applying a
  proposal is a separate, owner-gated decision that is not implemented.
- **Weekly learner failure as an unresolved required-path issue — NO LONGER
  APPLIES.** Weekly Anthropic commentary is now optional and **disabled by
  default** (`NARRATIVE_WEEKLY_COMMENTARY_ENABLED=False`). Its absence is
  reported as `OPTIONAL_COMMENTARY_DISABLED` and is explicitly not a learner
  failure.
- **"No unlock route / one-way-door hazard" — FIXED.** `PUT
  /api/narrative/strategy/{key}/lock` now sets or clears the lock **without
  touching the value**, with a required `reason` and an audit row. Editing a
  value no longer implies locking it. The constraint in §6 about not using the
  dashboard PUT to set parameters is therefore obsolete.
- **`LEARNER_SCHEDULING: ACTIVE` / `REVIVAL STATUS: BLOCKED_PENDING_LEARNER_FIX`
  — SUPERSEDED.** The current blocker is not the learner. It is (a) the provider
  billing state above, and (b) an *independent* strategy judgement: the lane is
  dispatch-quarantined on its own negative evidence (16% win rate, −$1,542 over
  six weeks, PR #437). **Restoring provider access would restore scoring, not the
  lane** — those are separate decisions, and no Anthropic top-up is warranted for
  this lane alone.

### Gates G1–G4 — historical

`G1`–`G4` below are **historical requirements**, not a current checklist. G4 was
already withdrawn by the in-document CORRECTION. G1 (learner proven functional)
is moot for the daily path, which no longer uses a provider. G2 is partially
delivered via #510/#514 for the learner and scorer paths; process-wide traceback
rendering remains open debt. G3 (explicit parameter re-baselining) survives as a
sensible precondition **if** the lane is ever revived — but revival is gated on
the strategy evidence, not on these gates.

### What remains valid

The measurements, the mutation-path inventory (§4), the narrowed claim (§5), and
the in-document CORRECTION about the sample gate being measured on the wrong
table are all retained as provenance. The forensic method — and the fact that
this document already caught and corrected one of its own errors — is why it is
superseded rather than deleted.

---

## Status block

```
*** HISTORICAL — state as of 2026-08-05. See SUPERSEDED block above. ***
NARRATIVE_PREDICTION_CURRENT_STATE: QUARANTINED        (still true 2026-08-07)
LEARNER_SCHEDULING: ACTIVE                             (superseded: daily path is
                                                        now deterministic/provider-free, #510)
QUALIFYING_SAMPLE: 932 / 100  (MET — 9.3x; see CORRECTION below)
LAST PARAMETER CHANGE: 2026-06-02
RESTRICTIVE_BOUND PARAMETERS: 2 / 14
CURRENT BEHAVIORAL IMPACT: NONE WHILE QUARANTINED      (still true)
REVIVAL STATUS: BLOCKED_PENDING_LEARNER_FIX            (SUPERSEDED: blocked by
                                                        provider billing AND by
                                                        independent negative
                                                        strategy evidence, PR #437)
```

---

## *** CORRECTION (2026-08-05, post-publication) — the sample gate was measured on the wrong table ***

An earlier revision of this document, and the status block it was built from,
recorded `QUALIFYING_SAMPLE: 5 / 100`. **That is wrong**, and the error is mine —
it propagated from an in-session claim into the status block.

**5** was the count of *narrative paper trades* since 2026-07-01. The learner does
not read `paper_trades`. Its sample gate is inside `apply_adjustments`
(`scout/narrative/learner.py:104-127`) and reads the **`predictions`** table:

```sql
SELECT COUNT(*) FROM predictions
 WHERE is_control = 0 AND outcome_class IS NOT NULL AND outcome_class != 'UNRESOLVED'
```

Actual value: **932**, against `min_sample = 100`. The requirement is **met by
9.3x**, and **877 already qualified before 2026-06-02** — the last successful
parameter change. The sample has *never* been the constraint.

`predictions` accumulate independently of paper-trade dispatch (3-33/day through
2026-08-04, resolving normally), so **the quarantine does not starve the learner.**

### What this invalidates

1. **There is no bootstrap paradox.** Gate G4 below was built on the premise that
   revival would require 100 outcomes generated through the frozen filter. That
   premise is false. No exploration phase, no lowered `min_sample`, and no staged
   widening is required. **G4 is withdrawn.**
2. **The two-condition claim in §5 is wrong.** It stated that restoring adaptation
   needs *both* a working learner *and* a sample the quarantine prevents
   accumulating. Only the first condition is real.
3. **The failure is upstream of the sample check entirely.** `learn.skip_adjustments`
   (INFO, step 6) fired **0 times in 7 days** while `learn.daily_error` fired **7**.
   The run crashes before `apply_adjustments` is reached — consistent with the
   retired-model hypothesis at step 4.

### Corrected conclusion

**Fix the learner and adaptation resumes immediately.** 932 qualifying predictions
are waiting; the next successful cycle can act on them. This is a materially
simpler and cheaper remediation than the document originally described.

The 7-day evidence window also supersedes the 3-day counts in §2:
`learn.daily_error` **7 / 7 days**, `learn.daily_complete` **0**,
`learn.weekly_complete` **0**.

---

## 1. Why the lane is silent — two independent gates

`signal_params.enabled = 1` for `narrative_prediction`. That is **not** the
operative gate.

**`SIGNAL_DISPATCH_QUARANTINE = ['narrative_prediction', 'tg_social']`**
(`scout/config.py:1054`, verified active in prod) blocks paper-trade opens at
`TradingEngine.open_trade` — SIG-03 dispatch-quarantine. Detection is
deliberately untouched; blocked opens are recorded to `trade_decision_events`
with `reason='quarantined'`.

Rationale in the code comment: *"Removes standing negative-EV lanes
(narrative_prediction 16% win / −$1,542/6w; tg_social 21% win / −$324) without
deleting their detection telemetry."*

Evidence the lane is otherwise alive:

| | |
|---|---|
| Narrative signals recorded since 2026-07-01 | **1,482** (30–74/day) |
| Narrative paper trades since 2026-07-01 | **5** |
| Last narrative paper trade | 2026-07-01 |

**§9c note.** `signal_params.enabled` is the lever nearest the outcome and it
reads "on". The controlling gate is one layer away in config. An earlier
in-session statement — "narrative_prediction is enabled but produced zero
trades" — was wrong for exactly this reason. Correct phrasing: *enabled at the
signal-params layer, quarantined at the dispatch layer.*

## 2. `learn.daily_error` — GENUINE LEARNER FAILURE, not sample-floor behaviour

**Classification: genuine failure.** The evidence is unambiguous.

| event (2026-08-02 → 08-05) | count |
|---|---|
| `learn.daily_error` | **3** (one per day) |
| `learn.weekly_error` | **1** |
| `learn.daily_complete` | **0** |
| `learn.daily_skip` | **0** |
| `narrative.daily_learn_complete` | 3 |

The sample-floor path is a **separate, INFO-level** log:
`log.info("learn.daily_skip", reason="no evaluated predictions")` and an early
`return None` (`scout/narrative/learner.py`, step 2 of `run_daily_learn`). It
fired **zero** times. What fires is the catch-all at
`scout/narrative/learner.py:340`:

```python
except Exception:
    log.exception("learn.daily_error")
    return None
```

**Zero successful learn cycles in the observed window.** The learner is not
starved into a clean skip — it is raising an exception on every run and
returning `None`.

The caller then logs `narrative.daily_learn_complete` regardless, so the failure
is invisible to anyone reading the completion event. That masking is why this
went unnoticed.

### ~~Root-cause hypothesis (HIGH confidence, NOT proven)~~ — **FALSIFIED 2026-08-07**

> Proven cause is `400 BILLING_BLOCKED` (exhausted credit balance), request
> `req_011CdooRRpdEKNVoRZx8cSZm`, reproduced on `NARRATIVE_SCORING_MODEL`
> — the model this section calls "current". It is credentials, and it is not
> path-specific. The paragraph below is retained as provenance only.

`NARRATIVE_LEARN_MODEL = claude-sonnet-4-6` — a **retired model ID**. Step 4 of
the daily learn calls Claude with `DAILY_REFLECTION_TEMPLATE`; a retired model
returns an API error, which the catch-all swallows.

Supporting evidence:
- `NARRATIVE_SCORING_MODEL = claude-haiku-4-5` is a **current** ID, and
  `ANTHROPIC_API_KEY` is present (len 108) — so this is not a credential
  failure, it is specific to the learn path's model.
- Last successful parameter change was **2026-06-02**, consistent with a model
  retirement rather than a data-availability change.

**Not proven**, and deliberately not proven: confirming it means running the
learner, which mutates parameters. That is out of scope under the current
instruction. See §6 for what would close it safely.

### Secondary defect — the traceback is discarded

`log.exception(...)` should carry `exc_info`, but the emitted record is
`{"event": "learn.daily_error", "level": "error"}` with **no exception, no
message, no fields**. `scout/main.py:2016` configures
`structlog.processors.JSONRenderer()` with no `format_exc_info` /
`dict_tracebacks` processor, so the traceback never reaches the log.

An error event carrying zero diagnostic content is its own defect, independent
of this lane: it is why a 63-day-old daily failure could not be diagnosed from
logs alone. **Severity: MEDIUM**, and it affects every `log.exception` call in
the process, not just this one.

## 3. Parameter state — 2 of 14 at a restrictive bound

Bounds are code-defined in `STRATEGY_BOUNDS` (`scout/narrative/strategy.py:44`).

| param | value | bound | position | set by | when |
|---|---|---|---|---|---|
| `laggard_min_volume` | 1,000,000 | (10k – 1M) | **AT MAX — strictest** | `learn_cycle_38` | 2026-05-24 |
| `max_picks_per_category` | 3 | (3 – 10) | **AT MIN — fewest picks** | `learn_cycle_8` | 2026-04-20 |
| `min_trigger_count` | 1 | (1 – 10) | at min — *most permissive* | `init` | never learned |

`min_trigger_count` is a seeded default at its loose end and is **not** a
ratchet. Only **2 of 14** bounded params sit at a restrictive extreme, and both
were learner-driven.

Scope check: only **5** params were ever learner-touched
(`laggard_min_change`, `laggard_min_mcap`, `counter_suppress_threshold`,
`laggard_min_volume`, `narrative_fit_score_min`, plus `max_picks_per_category`).
This is **not** a systemic runaway ratchet across the parameter set — it is two
frozen params plus a broken learner. The learner's failure is the more
consequential finding.

Resulting laggard filter, if the lane resumed today: mcap $1M–$200M, 24h change
**−5% to +10%**, volume **≥$1M**, max **3** picks/category.

## 4. Mutation paths — complete inventory (task 2)

| # | Path | Location | Bounds enforced | Lock enforced | Currently functional |
|---|---|---|---|---|---|
| 1 | `Strategy.set()` — learner | `strategy.py:105` | **Yes** (`STRATEGY_BOUNDS`) | Yes (raises) | **NO — crashes** |
| 2 | `PUT /api/narrative/strategy/{key}` — operator | `api.py:525` → `dashboard/db.py:419` | Yes (per docstring) | Yes (rejects locked) | **YES** |
| 3 | `Strategy.lock()` / `.unlock()` | `strategy.py:149/157` | n/a | n/a | Programmatic only — **no API route exposed** |
| 4 | Seed / init INSERT | `strategy.py:83` | n/a | n/a | First-run only |
| 5 | Direct SQL on `scout.db` | — | **No** | **No** | Yes, with host access |

Searched and **not found**: no migration writes values (`scout/db.py:2002` only
`CREATE TABLE IF NOT EXISTS`), no replay job, no seed script, no systemd unit,
no CLI tool under `scripts/` or `tools/`.

**One-way-door hazard.** Path 2 sets `locked = 1, updated_by = 'manual'`
(`dashboard/db.py:419-424`). Once locked, path 1 raises and path 2 rejects — and
**no unlock route is exposed**. A single manual dashboard edit therefore removes
that key from both supported paths permanently, reversible only by direct SQL or
programmatic `Strategy.unlock()`. Current state: **0 of 31 keys locked**, so
both paths are open today.

## 5. Narrowed claim (task 3)

An earlier in-session statement said these parameters are *"permanently frozen
and nothing in the current configuration can ever move them."* **That is too
strong and is retracted.**

Accurate statement:

> The two restrictive parameters are frozen **with respect to the automated
> learner, under the current runtime**, because the learner raises on every
> cycle. They remain mutable by an operator via
> `PUT /api/narrative/strategy/{key}` and by direct SQL. Restoring automated
> adaptation additionally requires ≥100 qualifying outcomes, which the dispatch
> quarantine currently prevents accumulating.

**CORRECTED (2026-08-05):** one condition, not two. **The learner is broken.** The
sample is *not* unavailable — 932 qualifying predictions exceed the 100 required.
~~Fixing the learner alone restores adaptation.~~

> **SUPERSEDED 2026-08-07 — the struck sentence is false.** The daily path now
> runs `run_deterministic_daily_learn` (#510), which is **proposal-only**: it
> writes a `learn_logs` provenance row and calls no `Strategy.set`. There is no
> automatic apply path, so *no* learner fix restores adaptation by itself.
> Applying a proposal is a separate, owner-gated decision that is deliberately
> not implemented. The 932-sample finding above remains correct.

## 6. Mandatory revival gate

The paradox to defeat: reviving the lane restarts it under **stale restrictive
parameters last tuned 2026-06-02 by a learner that has since failed every run**,
while the learner needs **100 outcomes generated through those same
restrictions** before it can adapt them. Left unaddressed, the lane would
under-perform for reasons unrelated to signal merit, and the evidence produced
would be an artefact of the frozen filter.

**No revival until all four gates pass. Each is independently blocking.**

**G1 — Learner proven functional.** Confirm the root cause and land a fix, then
observe **≥1 `learn.daily_complete`** in logs. Reviving before this means no
adaptation can occur at all. Safe confirmation without mutating prod: run the
daily-learn path against a **copy** of `scout.db` on a scratch path, or add the
missing `format_exc_info` processor first so the next scheduled run self-reports
its traceback. Do not run the learner against prod to diagnose it.

**G2 — Traceback rendering restored.** Add `format_exc_info` / `dict_tracebacks`
to the structlog chain (`scout/main.py:2016`). Without it, a repeat failure is
undiagnosable and G1 cannot be re-verified after any future regression. This is
worth doing regardless of the lane.

**G3 — Parameters explicitly re-baselined, not inherited.** Before revival,
either reseed the two restrictive params to defaults, or record an explicit
written justification for retaining them. Retaining `laggard_min_volume` at its
ceiling must be a **decision**, not an inheritance from a dead learner. Record
the chosen values and the reason in the revival record.

**G4 — WITHDRAWN.** See the CORRECTION at the top: the 932-prediction sample
already exceeds `min_sample=100` by 9.3x and accumulates independently of the
quarantine. No bootstrap plan is required. The original text is retained below,
struck through, only so the reasoning error remains auditable.

~~**G4 — Bootstrap plan for the 100-outcome sample.**~~ State up front how the
sample will be produced without the frozen filter biasing it. Acceptable
approaches, operator's choice:
  - **Exploration phase** — deliberately widened params for a bounded period to
    generate a representative sample, accepting that early outcomes are
    exploratory rather than performance evidence;
  - **Temporarily lowered `min_learn_sample`** (bounds 50–500; currently 100) to
    shorten the bootstrap, with the value restored afterwards;
  - **Staged widening** — revive at current params, and pre-register the
    widening trigger if fire-rate falls below a stated threshold.

An "unblock and watch" revival with no stated bootstrap plan **fails G4** — that
is precisely the shape that produced 63 days of silent failure.

**Additional constraint on G3/G4:** do not use path 2 (the dashboard PUT) to set
these values unless the one-way lock is acceptable. It sets `locked = 1` with no
exposed unlock, which would block the very learner G1 exists to restore.

## 7. Current behavioural impact: NONE

All of the above is inert while `SIGNAL_DISPATCH_QUARANTINE` contains
`narrative_prediction`. No trades open, no parameters are consulted for
admission, no money or paper capital is affected. Detection telemetry continues
normally (1,482 signals since 07-01) and is unaffected by any of this.

This is recorded as **dormant structural debt**: correct to leave alone, and
mandatory to resolve *before* — not during — any revival.

## 8. Not established

- The exact exception behind `learn.daily_error` (traceback suppressed; proving
  it requires running the learner, which mutates state).
- Whether the weekly learner (`learn.weekly_error`, 1 occurrence) fails for the
  same reason or a different one.
- Whether the 5 narrative trades after 2026-07-01 predate the quarantine or
  bypassed it — worth a look, not chased here.
- Whether `tg_social`, the other quarantined lane, carries equivalent debt. It
  is doubly blocked (`enabled=0` **and** quarantined) and was not examined.
