**New primitives introduced:** NONE. Design elaborates findings-doc structure + operator-decision presentation patterns for the audit defined in `tasks/plan_social_mentions_denominator_audit.md` (v2).

# social_mentions_24h Denominator Audit Design

**Companion to:** `tasks/plan_social_mentions_denominator_audit.md` (v2)

**Scope of this design:** Operator-facing presentation patterns for the findings doc (what the operator sees, in what order, with what hierarchy of recommendation-vs-uncertainty); follow-up backlog item shape (severity, owner, decision-by); integration choreography between the audit's outputs and downstream operator workflow.

---

## 1. Findings-doc structure (what the operator sees)

### 1a. Scan-order ordering

Operators scan top-down and exit early. Per Reviewer 3 PR #150 finding #5 ("fatigued operator scans verdict → reasons → SQL"), the findings doc must put **load-bearing facts** before **supporting evidence**. Ordering:

1. **TL;DR (1 paragraph)** — primary recommendation (Option B preferred) + headline empirical (0-flip blast radius across 6M+ rows) + status (defer code change to explicit operator approval)
2. **Empirical evidence (sectioned)** — corrected runtime-state verification table + 4 variant backtest results
3. **Open Questions (Reviewer 2 #8 fold)** — explicit operator-decision points that the audit surfaced but doesn't itself resolve
4. **Re-evaluation triggers** — 5 triggers (4 data-bound + 1 calendar backstop 2026-08-17)
5. **Cross-references** — backlog L228, prior `findings_bl032_social_signal_audit_2026_05_14.md`, memory `feedback_lunarcrush_dropped.md`

### 1b. Recommendation hierarchy presentation

The audit has THREE non-deferred options + ONE explicitly deferred option. Operator must distinguish "recommended" from "viable" from "deferred." Use a 3-tier callout block:

```markdown
## Recommendation

### PRIMARY — Option B (remove + recalibrate gates)
[2 sentences: why, with 0-flip evidence]
**Status:** Code change deferred to explicit operator approval; findings doc + cleanup comment ship now.

### SECONDARY — Option C (remove without recalibrating)
[2 sentences: when to choose this, the 35-promotion empirical]
**Status:** Viable if operator values funnel-widening.

### TERTIARY — Option A (defer entirely)
[2 sentences: when to choose this, the intellectual debt cost]
**Status:** Only if operator wants zero risk surface.

### DEFERRED — Option D (Hermes/TG bridge)
[2 sentences: why not eligible today]
**Status:** Data-readiness gate not met.
```

The visual hierarchy + status line per option lets operator skim and pick without re-reading.

### 1c. Open Questions section (Reviewer 2 #8 fold)

Format: numbered list of explicit decision points the audit surfaces but does not resolve. Each question:
- One sentence stating the choice the operator must make
- One sentence summarizing the evidence each direction
- "Recommended path: [B/C/A/D]; rationale: [...]"

Example questions to include:
1. **Preference between B (gate recalibration) and C (gate inflation widening)** — B keeps current friction (recommended); C unlocks 35 historical signals to MiroFish but adds review burden
2. **Acceptable cadence for re-evaluation** — current re-eval triggers are 4 data + 1 calendar (90d backstop); operator can adjust
3. **Whether to ship the one-line `# DEAD SIGNAL` comment now or defer** — current plan ships it; operator can elect to defer all `scout/scorer.py` touches

### 1d. Operator-acceptance-criterion verification checklist

Plan v2 self-review claims spec coverage. Findings doc should reproduce the operator's acceptance criteria (from the original prompt) verbatim with ✓/✗ + cross-link to evidence:

```markdown
## Operator acceptance criteria verification

| Criterion | Met? | Evidence |
|---|---|---|
| Quantifies score/ranking impact of removing the dead 15-point feature | ✓ | Variant B: 0 flips / 6M+ rows; Variant C: 35 promotions at MIN_SCORE |
| Identifies whether any profitable/missed signals would change ranking materially | ✓ | Top-10 historical = score 58 → variant_B 62; none reach CONVICTION 70 even after inflation |
| Documents Hermes-first result | ✓ | Category-exhaustive WebFetch + ecosystem 404 |
| Updates backlog/todo/memory/context | ✓ | Backlog status AUDITED + 4 follow-ups; todo board entry; memory checkpoint |
```

---

## 2. Follow-up backlog item shape

Each of the 4 follow-ups files inline at Task 3 commit time. Use the standard backlog entry shape (status / why / drift / Hermes / scope / decision-by):

### 2a. BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG

- **Status:** PROPOSED 2026-05-17
- **Why:** Re-eval triggers in BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT are operator-memory-dependent (Reviewer 1 #8 + Reviewer 2 #5 raised this as §12a-style silent-non-trigger risk)
- **Action:** Daily cron query against `narrative_alerts_inbound` resolution count + TG distinct-token rollup; alert operator on threshold crossing
- **Decision-by:** Conditional — file as cron addition when next infrastructure cycle ships

### 2b. BL-NEW-SCORER-DEAD-SIGNAL-COMMENT-CONVENTION

- **Status:** PROPOSED 2026-05-17
- **Why:** Reviewer 2 #2 flagged that Signal 13 has a documented gated-status comment (scorer.py:184-198) but Signal 5 doesn't. Future engineers see Signal 5 and assume it fires. Codify the convention.
- **Action:** Style guide entry + one-line PR adding `# DEAD SIGNAL`-class comments to any scorer signal whose gate hasn't fired in the last 7d
- **Decision-by:** Bundle with next scorer.py touch

### 2c. BL-NEW-TG-PER-TOKEN-ROLLUP-FEASIBILITY

- **Status:** PROPOSED 2026-05-17
- **Why:** Reviewer 2 #11 asked whether TG (1,746 messages / 290 in 7d / 51 with contracts) could replace Signal 5. Audit found 24h distinct-token rollup = 6 — insufficient. But this is the closest in-tree data source to the original Signal 5 intent. Worth periodic re-check.
- **Action:** Weekly cron printing `SELECT COUNT(DISTINCT contracts), COUNT(*) FROM tg_social_messages WHERE 24h GROUP BY chain`; alert operator when distinct-token count crosses 50/24h
- **Decision-by:** Bundle with BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG

### 2d. BL-NEW-SOCIAL-DENOMINATOR-OPERATOR-PREFERENCE

- **Status:** PROPOSED 2026-05-17
- **Why:** Reviewer 2 #3 raised operator preference between Variant B (precision) and Variant C (recall) as an explicit decision the audit surfaces but does not resolve.
- **Action:** Operator response to findings-doc Open Questions section. If B: schedule code change for next cycle. If C: schedule code change without gate recalibration. If A: close with deferral status.
- **Decision-by:** Operator response to PR #151 review

---

## 3. Integration choreography (downstream of findings doc)

### 3a. Operator reading flow

```
PR #151 / findings doc                           (this audit)
        │
        ↓ (operator reviews)
        │
   ┌────┴────┐
   │         │
chooses    chooses     chooses     chooses
   B         C            A           D
   ↓         ↓            ↓           ↓
[next PR]  [next PR]   [no PR]    [defer]
remove +    remove,     keep        wait for
recalibrate inflate     status      data-readiness
gates       gates       quo
   │         │            │           │
   ↓         ↓            ↓           ↓
runtime-state verify per §9a; gate change is live config flip per operator constraint;
require explicit pre-flight check on srilu DB state (recompute Variant B/C numbers against
latest scoring corpus)
```

### 3b. What this PR (PR #151) does and does not commit

**Commits (read-only / one-line cleanup):**
- Findings doc to `tasks/`
- Backlog status flip from PROPOSED → AUDITED 2026-05-17
- 4 follow-up backlog entries
- Todo board entry
- Memory checkpoint (lives outside repo)
- One-line `# DEAD SIGNAL` annotation on scorer.py:121 (zero behavior change; matches Signal 13 convention)

**Does NOT commit:**
- Variant B implementation (Settings change deferred to explicit operator approval)
- Variant C implementation (same)
- Hermes bridge (data not ready)
- Watchdog scripts (filed as follow-up backlog)

### 3c. Post-merge bookkeeping convention (per PR #150 Reviewer 1 discipline)

`AUDITED 2026-05-17` is the status; per BL-032 precedent at backlog L216 this is project convention for findings-only audits with no code-shipped resolution. Distinct from `SHIPPED <date>` which implies a code change landed.

---

## 4. Failure mode taxonomy

| Failure | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Operator overlooks Open Questions section and picks Option A by default | Medium | Medium (intellectual debt persists) | Findings doc TL;DR explicitly notes Option B is recommended; Open Questions section uses `### PRIMARY` callout |
| Operator picks Option C without realizing 35-candidate funnel widens manual-review load | Low | Low | Variant C section quantifies the load explicitly |
| Re-eval triggers never fire because no watchdog | Medium | Medium (audit rots) | Filed `BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG` follow-up + 90d calendar backstop |
| `# DEAD SIGNAL` comment becomes stale if Signal 5 ever re-wires | Low | Low | Comment refs the audit ticket; updating the ticket will trigger a comment update at scorer.py touch time |
| Variant B's "0-flip" claim invalidated by future scoring distribution change | Low | Low | The claim is current-corpus-bound; new scoring data after BL-053/BL-054/BL-NEW-QUOTE-PAIR changes is unlikely to push past max 58 quickly; re-eval triggers cover this |
| Audit findings get cited as authoritative for future BL-NEW-SOCIAL-* decisions despite stale data | Medium | Medium | Findings doc dated 2026-05-17 in filename + headers; 90d calendar backstop forces re-audit |

---

## 5. Rollback / disable semantics

This PR has **no runtime side-effects** (one-line code comment is zero-behavior-change). Rollback = revert the PR. No state to clean up.

If operator later approves Variant B and ships the code change in a separate PR, that PR will have its own rollback semantics (revert + Settings .env override to restore old gates).

---

## 6. Operator runbook (post-merge action)

### 6a. Reviewing the findings doc

```bash
# View the findings
gh pr view 151                                              # PR overview
cat tasks/findings_social_mentions_denominator_audit_2026_05_17.md
```

### 6b. Responding to Open Questions

Operator response paths (file as PR-151 comment OR new BL- item):

1. **Pick Option B** → comment "Approving Variant B for next-cycle implementation. Recalibrate MIN_SCORE 60→65, CONVICTION_THRESHOLD 70→75." This triggers next PR cycle implementing the code change.
2. **Pick Option C** → comment "Approving Variant C; accept funnel widening of 35 historical candidates at MIN_SCORE." Next PR removes Signal 5 without gate recalibration.
3. **Pick Option A (defer)** → comment "Defer all code change; re-eval at 2026-08-17 calendar backstop." No further action this cycle.
4. **Pick Option D (await Hermes data)** → comment "Defer pending bridge gate satisfaction." Watchdog will trigger re-eval.

### 6c. Manual verification before implementing B or C

```bash
# Re-run the audit against fresh srilu DB before any code change
ssh srilu-vps 'sqlite3 /root/gecko-alpha/scout.db < /tmp/audit_v2.sql'
# Confirm: max(quant_score) still < CONVICTION_THRESHOLD; Variant B flip count still 0
```

---

## 7. Self-review checklist

- [ ] Plan v2 enumerated all primitives; this design adds zero (✓ — header asserts NONE)
- [ ] Findings doc structure (§1) addresses all R1/R2 findings re. operator scan order
- [ ] Recommendation hierarchy (§1b) makes B/C/A/D distinction visible
- [ ] Open Questions section explicit (§1c)
- [ ] Operator acceptance criteria verification table (§1d)
- [ ] 4 follow-up backlog items have decision-by + action (§2)
- [ ] Choreography diagram shows downstream operator paths (§3a)
- [ ] PR scope vs out-of-scope explicit (§3b)
- [ ] AUDITED vs SHIPPED status convention named (§3c)
- [ ] Failure modes tabulated with mitigations (§4)
- [ ] Rollback semantics defined (§5)
- [ ] Operator runbook concrete enough to execute (§6)
