**New primitives introduced:** NONE — docs-only changes to `tasks/todo.md` and (if evidence supports) `backlog.md`. No code, no schema, no scripts, no settings.

# Overnight Drift-Cleanup Implementation Plan v2 (2026-05-19)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan in this session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan-review fold history:**
- v1 → v2 (2026-05-19): Reviewer A (evidence-rigor) flagged TWO CRITICAL — bulk-delete assumption + phantom backlog grep for PR #82. Reviewer B (scope/blast-radius) flagged TWO IMPORTANT — bulk-delete vs operator's "mark or move" verbs + pre-existing `M tasks/lessons.md`. Both reviewers converged on the bulk-delete issue from orthogonal axes. v2 folds: (1) bulk delete narrowed to verified-duplicate header ranges only (`## Historical (pre-cycle-14)` collapse for the rest); (2) PR #82 evidence cites todo.md:477 (the actual `[x]` line), not a phantom backlog entry; (3) gainers_early closure pre-committed to ELAPSED-AUTO-SUSPENDED per backlog L1796+1798 (memory `project_soak_closure_2026_05_13.md` is pre-PR-#150-evaluator and explicitly contradicted by the new evaluator); (4) Task 1 adds `git diff tasks/lessons.md` baseline check; (5) Task 2 prelude adds line-number re-derivation via grep; (6) Task 4 Step 1 tightens runtime-state-unverifiable note; (7) Task 6 adds `git status --short` pre-commit; (8) Reviewer C in Task 8 swapped to structural axis (Markdown validity + SQL block preservation) per Reviewer B N1.

**Goal:** Surface and close stale unchecked items in `tasks/todo.md` (Priority 1 of operator's overnight assignment) by citing file:line / PR / DB / log / memory evidence; collapse duplicate sections; preserve genuinely live items in a clear current section. Verify `backlog.md` status accuracy on a narrow named set (Priority 1b) and produce a one-line evidence-back note per entry.

**Architecture:**
- Docs-only PR. No code, schema, scripts, or settings change. No live config or secrets touched.
- Apply two heuristics from CLAUDE.md §7a (drift-check) and §9c (lever-vs-data-path):
  - Tick / move only when in-tree evidence (file:line, PR sha, backlog status, memory file) supports closure.
  - For each closure, the evidence citation goes inline in todo.md so a future session can re-verify in seconds.
- Preserve everything genuinely operator-gated, evidence-gated, date-gated as `[ ]` and either leave it in place OR move into a single explicit `## Outstanding (live, NOT closed)` section.
- Duplicate stale sections (lines 248-519 of pre-edit todo.md) collapse into the head copy — no information loss because the older copy strictly precedes the head copy in content.

**Tech Stack:** Edit tool against markdown. Read tool for evidence verification (backlog.md, memory MEMORY.md, git log on master, recent merge SHAs).

---

## Constraints (operator's explicit scope guardrails)

- Do NOT touch evidence/date-gated items: #158 24h validation; `/coins/{id}` fallback; stale-count alert baseline; June-gated decisions; HPF n≥20; first_signal 2026-05-31; Minara D+14.
- Do NOT execute live config or secrets (no `.env` edits, no `secrets.token_hex`, no SKILL.md edits).
- Do NOT modify backlog status without file:line + commit evidence — bias to "leave as-is and document" over "flip optimistically".
- Bias to surgical edits over restructuring — preserve the operator's verification-query SQL blocks and operator-pastable runbook commands verbatim.

---

## File Structure

**Files modified in this PR:**
- `tasks/todo.md` — close stale unchecked items inline with evidence; collapse lines 248-519 duplicate block.
- `backlog.md` — touched ONLY if a status field is evidence-confirmed wrong. Else untouched.
- `tasks/lessons.md` — append one short lesson if cleanup discovers a new pattern worth saving.
- `~/.claude/projects/C--projects-gecko-alpha/memory/MEMORY.md` — add one cleanup checkpoint entry pointing at this PR (worktree path: `C:\Users\srini\.claude\projects\C--projects-gecko-alpha\memory`).

**Files NOT modified:**
- Any `scout/` source, any `tests/`, any `scripts/`, any `cron/`, any `systemd/`, any `dashboard/`, any `.env*`, any `pyproject.toml`. If the audit surfaces a needed code change, file as follow-up backlog entry — do NOT include in this PR.

---

## Task 1: Verify worktree + baseline state

**Files:** None (read-only).

- [ ] **Step 1: Confirm worktree HEAD == origin/master and tree minus the plan file is clean**

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/master
```

Expected: branch is `worktree-overnight-drift-cleanup`; HEAD SHA matches `origin/master`; only diff is the new plan file `tasks/plan_drift_cleanup_2026_05_19.md` (untracked).

- [ ] **Step 1b: Inspect baseline-modified `tasks/lessons.md` (R-B I2 fold)**

```bash
git diff tasks/lessons.md
```

The baseline `git status` at session start showed `M tasks/lessons.md`. If the diff content is unrelated to this audit (e.g., a stray edit from a prior session), either (a) commit it as a SEPARATE preliminary commit with conventional message AND scope the audit commit to `tasks/todo.md` only, OR (b) document the pre-existing diff in the audit commit body with an explicit citation. Do NOT silently fold pre-existing edits into the docs-cleanup commit via `git add tasks/lessons.md 2>/dev/null || true`.

If the lessons.md diff IS audit-related (e.g., from a prior aborted attempt at this same work), discard it: `git checkout tasks/lessons.md`.

- [ ] **Step 2: Re-derive todo.md unchecked-item line map (R-A I4 fold)**

```bash
grep -n '^- \[ \]' tasks/todo.md
awk 'END{print NR}' tasks/todo.md
```

Capture the exact set of unchecked-item line numbers in current `tasks/todo.md`. The earlier plan v1 embedded line numbers from a stale read; v2 re-derives them at execution time.

- [ ] **Step 3: Test the duplicate-block hypothesis with concrete diffs (R-A C1 fold)**

```bash
diff <(sed -n '5,90p' tasks/todo.md) <(sed -n '277,363p' tasks/todo.md)
diff <(sed -n '93,110p' tasks/todo.md) <(sed -n '365,382p' tasks/todo.md)
diff <(sed -n '149,205p' tasks/todo.md) <(sed -n '421,479p' tasks/todo.md)
```

Reviewer A confirmed via this exact diff that lines 5-90 are NOT a strict superset of 277-363 — the head is newer cycle-15 content (NARRATIVE-OPERATOR-ALERT-WIRE, CHAIN-ANCHOR, HELIUS/MORALIS, CG-LANE-ORDER) and the tail is older cycle-9/10 content (baseline test failures after PR #136). The "strict subset" assumption from plan v1 was WRONG. v2 narrows the deletion scope accordingly (see Task 3 Step 3 v2).

Run the three diffs and capture which ranges are TRULY duplicate (e.g., identical or trivially-different content) vs which are non-duplicate-but-stale (older state of the same item) vs which are non-duplicate-and-unique (one-off operator notes / SQL blocks not present in head).

---

## Task 2: Build the evidence dossier (read-only)

**Files:** None (read-only).

For each candidate-closure item, capture the exact evidence string (PR #, commit SHA, file:line, memory file name, or backlog status line). The string goes INLINE into todo.md at closure time so future sessions can re-verify in seconds.

- [ ] **Step 1: BL-NEW-QUOTE-PAIR soak D+3 / D+7 (lines 95-96, 367-368)**

```bash
grep -n 'BL-NEW-QUOTE-PAIR' backlog.md | head -5
sed -n '139,151p' backlog.md
```

Expected: `### BL-NEW-QUOTE-PAIR` SHIPPED 2026-05-09 via PR #85 (`3774591`); soak D+0=2026-05-09 to D+7=2026-05-16. Today is 2026-05-19 — soak ended 3 days ago. Need to determine whether revert fired. Memory `project_bl_quote_pair_2026_05_09.md` says "Revert via `STABLE_PAIRED_BONUS=0` env override if alert volume > +10% baseline".

Evidence string to embed (drafting now): `Soak ended 2026-05-16 (3d ago at audit time 2026-05-19). Per backlog.md §BL-NEW-QUOTE-PAIR (SHIPPED 2026-05-09 PR #85 \`3774591\`) and memory \`project_bl_quote_pair_2026_05_09.md\`, no revert trigger fired in source-of-truth docs and STABLE_PAIRED_BONUS remains the default magnitude. Closing both D+3 and D+7 as ELAPSED-WITHOUT-REVERT.` If backlog doesn't carry a final disposition row, file one-line `BL-NEW-QUOTE-PAIR-SOAK-CLOSURE` follow-up in backlog.md.

- [ ] **Step 2: Paper-lifecycle widening soak (lines 102, 147, 374, 419)**

```bash
grep -n 'paper.lifecycle\|paper-lifecycle' backlog.md
grep -n 'PAPER_MAX_DURATION_HOURS\|PAPER_SL_PCT' backlog.md | head -5
```

Soak ended 2026-05-04T22:24Z — 15 days past. Sneak-peek decision in todo.md text already says "keep on" (line 102 inline text: "Sneak-peek +$1,234 net / 91 closes. Decision: keep on."). No evidence of revert. Close as KEEP-ON-CONFIRMED with citation to the inline sneak-peek decision.

- [ ] **Step 3: PR #59 strategy tuning soak (lines 103, 143, 375, 415)**

```bash
git log --oneline | grep -i 'PR #59\|strategy tuning\|3c83fb7' | head -5
sed -n '215,225p' tasks/todo.md
```

Soak ended 2026-05-05T22:58Z — 14 days past. Inline text says "Sneak-peek +$1,994 net / 135 closes / 67.4% win / 20% expired. Decision: keep on permanently." `What shipped this session` block (line 484-489) lists PR #59 as `3c83fb7`. Close as KEEP-ON-PERMANENTLY with citation. The duplicate at line 143 is part of the lines-248-519 collapse.

- [ ] **Step 4: gainers_early reversal re-soak 7d (R-A I3 pre-commit fold)**

Soak ended 2026-05-17. Reviewer A confirmed the disambiguation is pre-determined by evidence already in backlog.md:
- Backlog L1796: "gainers_early=FAIL contradicting 2026-05-13 audit-id=24"
- Backlog L1798: "auto-suspend firing 2026-05-17T01:02:46Z (audit ids 26/27)"
- Memory `project_soak_closure_2026_05_13.md` (KEEP-ON audit-id=24) is *pre-PR-#150-evaluator* and explicitly contradicted by the new evaluator shipped 2026-05-17.

Evidence string to embed (pre-committed): `**CLOSED 2026-05-19: ELAPSED-AUTO-SUSPENDED. Re-soak ran 7d 2026-05-10 to 2026-05-17. Per backlog.md L1796 (BL-NEW-LC-REVIVAL-CRITERIA-TIGHTENING) the new evaluator returned FAIL contradicting the 2026-05-13 audit-id=24 KEEP-ON verdict, and backlog.md L1798 records auto-suspend firing 2026-05-17T01:02:46Z (audit ids 26/27). Memory \`project_soak_closure_2026_05_13.md\` reflects the pre-PR-#150-evaluator verdict that was explicitly contradicted by the new evaluator.**`

- [ ] **Step 5: PR #82 BL-NEW-MOONSHOT-OPT-OUT deploy (R-A C2 fold — cite todo.md L477, not phantom backlog entry)**

Reviewer A confirmed that backlog.md has NO `BL-NEW-MOONSHOT-OPT-OUT` entry; the only inline closure record is in `tasks/todo.md` itself at line 477 which says `[x] moonshot floor nullification — UPSTREAM FIX MERGED 2026-05-06 (PR #82, deploy held until 2026-05-13)`.

```bash
sed -n '475,478p' tasks/todo.md
git log --oneline --all | grep -i 'moonshot\|signal_params.*moonshot\|BL-NEW-MOONSHOT-OPT-OUT' | head -10
```

The merge is confirmed (PR #82). The deploy state is NOT verifiable in a docs-only PR (no SSH per scope). Correct closure:

Evidence string: `**CLOSED 2026-05-19: PR #82 MERGED 2026-05-06 per todo.md L477 (corresponding head-section [ ] at L108 was stale). Migration adds default-opt-IN flag (\`moonshot_enabled INTEGER NOT NULL DEFAULT 1\`) — zero behavior change on deploy. Per-signal opt-out remains operator-driven via UPDATE on \`signal_params\`. Deploy state on srilu is operator's responsibility per the runbook and is unverifiable in this docs-only PR.**`

- [ ] **Step 6: chain_complete fire-rate observation post-PR #80 (line 109)**

Already CLOSED at line 381 with full evidence ("Lifetime: full_conviction=201..."). The duplicate at line 109 should be marked `[x]` pointing to line 381's evidence — OR removed in the duplicate-block collapse if line 109 IS the line-248-519 duplicate. Verify.

```bash
sed -n '380,382p' tasks/todo.md
sed -n '108,110p' tasks/todo.md
```

- [ ] **Step 7: RE-SCOPED system health checkpoint 2026-05-15 (line 115)**

Checkpoint date elapsed 4 days ago. Looking at content: this is a user-driven 14d strategic-checkpoint question set (P&L re-baseline, Tier 1a infrastructure health, next-best-next decision). It's NOT a soak with a kill-criterion — it's a "schedule a conversation" reminder. Operator hasn't surfaced a memory or backlog entry indicating the checkpoint conversation happened. Options:

  (a) Leave as `[ ]` because the checkpoint is genuinely pending operator review.
  (b) Move under "Outstanding (live)" with date-elapsed flag and a note: "Checkpoint date 2026-05-15 elapsed at audit time; SQL queries below remain valid for operator's next session."
  
Pick (b) — preserves the SQL queries (operator-pastable) and signals the date-elapsed status without false-closing an open decision.

- [ ] **Step 8: PR #58 BL-064 lenient-safety soak (line 141)**

Re-check window 2026-05-12 elapsed 7 days. Inline text says "As of 2026-04-29T12:25Z: 0 trades dispatched yet (curators haven't posted CA-bearing messages since flag flipped). Operational gap, not code." Need to check whether any BL-064 trades have fired since:

```bash
grep -n 'BL-064\|TG_SOCIAL_ENABLED\|tg.social' backlog.md | head -20
grep -n 'safety_required' backlog.md | head -5
```

Memory `project_bl064_deployed_2026_04_27.md` documents bootstrap; memory `project_narrative_scanner_v1_1_shipped_2026_05_13.md` documents follow-on KOL list. Likely closure stance: "operational-gap-elapsed; re-check defer to next operator-initiated BL-064 retrospective". Leave as `[ ]` under "Outstanding (live)" with date-elapsed note.

- [ ] **Step 9: BL-063 moonshot soak (line 145)**

Already CLOSED at line 101/373 (KEEP ON PERMANENTLY). Duplicate; collapse in line-248-519 cleanup.

- [ ] **Step 10: BL-064 14d TG social soak (line 146)**

Memory `project_trending_catch_soak_2026_05_10.md` says trending_catch auto-killed 2026-05-11. That's a DIFFERENT soak from BL-064's 14d. BL-064 14d ended 2026-05-11T22:10Z — 8 days past. Need to check whether BL-064 produced any dispatches in the soak window.

```bash
grep -n 'BL-064.*14d\|BL-064.*soak' backlog.md | head -10
```

If no dispatches fired (operational-gap), close as ELAPSED-OPERATIONAL-GAP. If dispatches fired, close per their outcome. If unclear, leave as `[ ]` under "Outstanding (live)".

- [ ] **Step 11: narrative_prediction token_id divergence (line 198, 470)**

Already CLOSED at line 475 (PR #80 / `eaf3523`). Duplicate at 198/470 should be ticked + collapse the 248-519 block.

- [ ] **Step 12: Audit fix #4 (24h hard-exit if peak<5%) (line 200, 472)**

Inline text says "deferred — accumulate more data first." Genuinely pending; leave as `[ ]` under "Outstanding (live)".

- [ ] **Step 13: first_signal revival decision (line 206, 478)**

Backlog §BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION line 1791 says "SHIPPED-WITH-DECISION 2026-05-17... Option A REVIVE-AND-SOAK with 14d window ending 2026-05-31. Memory checkpoint: `project_first_signal_revival_decision_2026_05_31.md`." Close line 206/478 as DECIDED-REVIVE-AND-SOAK with backlog + memory citation; the 14d soak end date (2026-05-31) is per the operator's explicit "do NOT start early" guardrail so we don't pre-close the soak result.

- [ ] **Step 14: BL-NEW-CHAIN-COHERENCE / chain pattern alert_priority (memory said reverted)**

Memory `project_chain_completed_priority_revert_2026_05_17.md` says "PR #146 snapshot-restore already had prod at low". This is post-status — no todo.md unchecked item to close, just inform the cleanup.

---

## Task 3: Draft the todo.md cleanup edit

**Files:** Modify `tasks/todo.md` (single file, single PR).

**Structural shape of the cleaned file:**

```markdown
# Backlog — gecko-alpha

Last updated: 2026-05-19 (cycle 15: overnight drift-cleanup — closed N stale unchecked items, collapsed duplicate block, preserved live items)

## Active Work: [cycle 14 entries — keep lines 5-247 as-is, they're current]

[All lines 5-247 preserved bit-for-bit.]

## Outstanding (live, NOT closed)

[Items where closure is NOT evidence-supported — genuinely operator-gated, evidence-gated, or date-gated. From Task 2 above:]

- [ ] **PR #82 BL-NEW-MOONSHOT-OPT-OUT deploy** [if Step 5 finds no deploy evidence; else closed]
- [ ] **2026-05-15 system health checkpoint** [Step 7 — operator decision deferred]
- [ ] **PR #58 BL-064 lenient-safety soak re-check** [Step 8 — operational-gap pending]
- [ ] **BL-064 14d TG social soak result** [Step 10 — if dispatches not surfaced]
- [ ] **Audit fix #4 (24h hard-exit if peak<5%)** [Step 12 — deferred per inline text]
- [ ] **first_signal revival 14d soak ends 2026-05-31** [Step 13 — operator's "do NOT start early" guardrail]
- [ ] **narrative_prediction token_id divergence — upstream batch 2** [if a second batch is documented elsewhere; else removed]

## Closed this audit (2026-05-19)

[Each row gets one line with inline evidence — file:line / PR # / commit SHA / memory file / backlog status.]

- [x] **BL-NEW-QUOTE-PAIR soak D+3 / D+7** — Step 1 evidence.
- [x] **Paper-lifecycle widening soak** — Step 2 evidence.
- [x] **PR #59 strategy tuning soak** — Step 3 evidence.
- [x] **gainers_early reversal re-soak** — Step 4 evidence (KEEP-ON or AUTO-SUSPENDED based on Step 4 outcome).
- [x] **chain_complete fire-rate observation** — Step 6: already closed at line 381; duplicate collapsed.
- [x] **BL-063 moonshot soak** — Step 9: already closed at line 101/373; duplicate collapsed.
- [x] **narrative_prediction token_id divergence** — Step 11: PR #80 / `eaf3523` 2026-05-06; line 475 has full evidence; duplicate at line 198/470 ticked.
- [x] **first_signal revival decision** — Step 13: backlog §BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION; SHIPPED-WITH-DECISION 2026-05-17 Option A REVIVE-AND-SOAK 14d ends 2026-05-31.

## What shipped recently

[Preserve operator's "What shipped this session" table verbatim — operator's record of PR history is high-value substrate.]
```

- [ ] **Step 1: Read current todo.md head sections (lines 1-247)**

Identify exactly which `[ ]` lines in the head sections (NOT the duplicate block 248-519) need closure. From Task 2 above, the head copies of these unchecked items are at lines 95-96, 102, 103, 104, 108, 109, 115, 141, 145, 146, 147, 198, 200, 206. Some are duplicates between head sections themselves — confirm by re-reading.

- [ ] **Step 2: Apply Edit operations to close ticked items inline**

For each evidence-supported closure, change `- [ ]` → `- [x]` AND append a parenthetical: ` — CLOSED 2026-05-19: <evidence string from Task 2>`. Keep the original date / context text intact so the historical record is preserved.

Example transformation (Step 4 BL-NEW-QUOTE-PAIR D+7):

Before:
```
- [ ] **D+7 soak end** — alert volume must not exceed +10% baseline. Revert via `STABLE_PAIRED_BONUS=0` env override if breached.
```

After:
```
- [x] **D+7 soak end** — alert volume must not exceed +10% baseline. Revert via `STABLE_PAIRED_BONUS=0` env override if breached. **CLOSED 2026-05-19: soak ended 2026-05-16 (3d before audit); no revert trigger fired per backlog.md §BL-NEW-QUOTE-PAIR (SHIPPED 2026-05-09 PR #85 `3774591`) and memory `project_bl_quote_pair_2026_05_09.md`.**
```

- [ ] **Step 3 v2: Treat the 248-519 tail as HISTORICAL, not duplicate (R-A C1 + R-B I1 converged fold)**

Two reviewers from orthogonal axes both flagged the original "bulk delete" plan as wrong. Reviewer A: the diffs show the tail is NEWER vs OLDER cycle content, not duplicate. Reviewer B: operator's verbs are "mark" and "move," not "delete" — a 270-line bulk delete is mechanically out-of-shape for a docs-cleanup PR.

**v2 deletion shape (per-section verified-duplicate ranges only):**

After Task 1 Step 3 captures the actual diff output, apply the following decision rule per section:

| Tail section | Decision rule | Action |
|---|---|---|
| Identical-or-trivial diff against a head section | Verified duplicate | Delete the tail copy. |
| Same item, older state (head has [x] with newer evidence; tail has [ ] from older cycle) | Stale-but-superseded | Delete the tail copy after confirming the head copy is the authoritative state. |
| Unique content not present in head (operator SQL block, one-off note, historical decision record) | NOT a duplicate | Keep — move under `## Historical (pre-cycle-14)` collapsed section at the bottom of the cleaned file. |

**Implementation tactic:** rather than `sed -i '248,519d'`, do per-section deletions using Edit operations. Each Edit either (a) deletes a confirmed-duplicate header through to the next header, OR (b) moves a non-duplicate section under the `## Historical` heading.

**Defensive structural preservation:** every operator-pastable code block (SQL queries, runbook commands) MUST be preserved — under `## Outstanding (live)` if its parent item is open, under `## Historical (pre-cycle-14)` if its parent item is closed but the SQL is still operationally useful.

**Markdown validity check:** after the per-section edits, run `awk 'END{print NR}' tasks/todo.md` and `grep -c '^## ' tasks/todo.md`; spot-check the headings list parses to a clean tree with no orphaned content.

**Reversibility:** the worktree branch ships as a single commit; if a 3-reviewer PR-stage check flags an unintended deletion, `git revert <sha>` fully restores. The choice of per-section Edit ops over bulk-sed makes intermediate review easier.

- [ ] **Step 4: Add cleanup metadata header + outstanding-section + closed-section**

After line 3 (Last-updated stamp), update the stamp to today's date and append an explanation. Add the two new sections per the structural shape above. Maintain the operator's "What shipped this session" table.

- [ ] **Step 5: Run a final readability pass**

```bash
awk 'END{print NR}' tasks/todo.md
```

Target: file should be 200-300 lines after cleanup (was 519). Spot-check that all `[ ]` items remaining are intentional (operator-gated, evidence-gated, or date-gated).

---

## Task 4: backlog.md narrow status verification (Priority 1b)

**Files:** `backlog.md` — touched ONLY if evidence confirms a status field is wrong.

For each operator-named entry in Priority 2-3 (Steps below), produce a one-line note. If status is correct as-is, no edit. If status is wrong, propose the corrected status in this PR with citation.

- [ ] **Step 1: BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE** (backlog L905-921)

Current status: `ENDPOINT-SHIPPED / HERMES-SKILL-PENDING 2026-05-18 — PR #176 merged 012e67c`. Per operator scope: "Verify endpoint is still live and 503-gated only by empty/missing OPERATOR_ALERT_HMAC_SECRET." Read-only verification: `grep -n OPERATOR_ALERT_HMAC_SECRET scout/api/internal_alert.py scout/config.py`; verify the gate semantics in code.

**R-A I5 fold:** code-side gate verification ONLY. Runtime state on srilu (whether `OPERATOR_ALERT_HMAC_SECRET` is set or empty) is not verifiable in this docs-only PR (no SSH per scope). The activation runbook at `tasks/runbook_operator_alert_activation_2026_05_19.md` carries the operator's runtime responsibility. If code-side gate semantics confirm the empty-secret → 503 path, status is correct → no edit.

- [ ] **Step 2: BL-NEW-CRON-DRIFT-WATCHDOG** (backlog L1680-1685)

Current status: `SCRIPT-SHIPPED / SCHEDULING-PENDING-OPERATOR 2026-05-18 — PR #156 merged 7f9aee6`. Operator hasn't scheduled. Status correct → no edit.

- [ ] **Step 3: BL-NEW-CG-FREE-TIER-DEMO-API-KEY** (backlog L895-903)

Current status: `RUNBOOK-READY 2026-05-18`. Runbook at `tasks/runbook_cg_demo_api_key_2026_05_18.md`. Read-only verification: confirm runbook file exists and references the 5+1 ingestion sites + 4 indirect sites. Status correct → no edit.

- [ ] **Step 4: BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-OPERATOR-PREFERENCE** (backlog L257-262)

Current status: `PROPOSED 2026-05-17 — pending operator B vs C decision`. Operator decision still open. Status correct → no edit.

- [ ] **Step 5: BL-NEW-MORALIS-ENABLEMENT-GUARDRAIL** (backlog L1009-1022)

Current status: `PROPOSED 2026-05-18 — conditional on operator intent`. Status correct → no edit.

- [ ] **Step 6: BL-NEW-HELIUS-ENABLEMENT-GUARDRAIL** (backlog L988-1000)

Current status: `PROPOSED 2026-05-18 — conditional on operator intent`. Status correct → no edit.

- [ ] **Step 7: BL-NEW-BL060-CYCLE-VERIFY** (backlog L1061-1064)

Current status: `PROPOSED 2026-05-13`. Operator scope says "audit whether BL-060 paper-mirrors-live pacing is cycle-independent and not anchored to old 15-min cycle assumption. Drift-check first; this may already be solved." This is findings-first — do NOT promote to PR-OPEN. Drift-check is in Task 5 below.

- [ ] **Step 8: BL-NEW-REVIVAL-VERDICT-WATCHDOG** (backlog L1802-1807)

Current status: `PROPOSED 2026-05-17`. Operator scope says "Assess whether provisional revival verdict expiry can silently go stale." Audit-only — do NOT build. Findings deferral covered in Task 5.

- [ ] **Step 9: BL-NEW-SCORER-DEAD-SIGNAL-COMMENT-CONVENTION** (backlog L250-255)

Current status: `PROPOSED 2026-05-17`. Operator scope says "Bundle with scorer touch only if another scorer PR happens; otherwise docs-only PR is fine." Defer unless other Task surfaces a scorer touch.

- [ ] **Step 10: BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG** (backlog L243-248)

Current status: `PROPOSED 2026-05-17`. Operator scope says "Audit whether a watchdog for re-eval triggers is still useful independently. If tied too tightly to the pending B/C decision, document and defer."

- [ ] **Step 11: Stale PR triage (#105, #34, #33)**

Per todo.md L69-78 + memory `project_stale_pr_triage_2026_05_18.md`, all three are flagged "STILL VALUABLE — left open with rebase recommendation comment". Operator scope says "do NOT do large rebases by default. Comment with evidence. Close if clearly obsolete/superseded. If still valuable but large, leave operator recommendation." Action already taken; status correct → no edit.

---

## Task 5: Surface findings-only audits (do NOT build code)

For the items where operator scope says "findings-first" or "audit-only", produce short audit notes inside `tasks/todo.md` under a new "Audit findings (this session)" subsection — OR file separately as `tasks/findings_overnight_drift_cleanup_2026_05_19.md` if the findings grow beyond ~30 lines.

- [ ] **Step 1: BL-NEW-BL060-CYCLE-VERIFY drift-check**

```bash
grep -rn 'BL-060\|paper.mirror\|paper-mirrors-live\|would_be_live' scout/ | head -20
grep -n 'INTERVAL\|cycle\|MINUTE\|15' tasks/bl060-paper-mirrors-live-design.md 2>/dev/null | head -10
```

Determine whether BL-060 pacing reads from cycle-period or a hard-coded 15-min assumption. Document finding. If cycle-independent → close BL-NEW-BL060-CYCLE-VERIFY as audit-confirmed. If hard-coded → leave as PROPOSED with findings note.

- [ ] **Step 2: BL-NEW-REVIVAL-VERDICT-WATCHDOG drift-check**

```bash
grep -rn 'keep_on_provisional_until\|soak_verdict' scout/ scripts/ systemd/ | head -20
grep -rn 'signal_params_audit.*field_name' scout/ | head -10
```

Determine whether any existing primitive can be reused (e.g., existing cron watchdog, calibrate.py periodic run, dashboard endpoint that surfaces stale verdicts) before scoping fresh build. Document finding inline.

- [ ] **Step 3: BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG drift-check**

```bash
grep -rn 'narrative_alerts_inbound.resolved_coin_id\|tg_social_messages' scripts/ systemd/ cron/ dashboard/ | head -10
```

Determine whether existing watchdog patterns or dashboard endpoints already surface the re-eval triggers. Document finding.

- [ ] **Step 4: BL-NEW-SCORER-DEAD-SIGNAL-COMMENT-CONVENTION**

```bash
grep -n '# DEAD SIGNAL\|# GATED' scout/scorer.py
```

Check if existing comments already follow the proposed convention. If yes, no PR needed — backlog entry can be CLOSED as IN-TREE. If no, defer per operator scope ("bundle with next scorer touch").

---

## Task 6: Commit + push

**Files:** Git commit log.

- [ ] **Step 1: Stage explicitly-named files (R-B N2 + I2 fold)**

```bash
git status --short                  # confirm only audit-related files are dirty
git add tasks/todo.md
git add tasks/plan_drift_cleanup_2026_05_19.md
# If Task 1 Step 1b committed lessons.md separately, do not re-stage.
# If Task 1 Step 1b decided to fold the baseline diff in, document in body.
# DO NOT git add -A (operator memory: "prefer adding specific files by name")
git status --short                  # second check before commit
```

- [ ] **Step 2: Commit with conventional message**

```bash
git commit -m "$(cat <<'EOF'
docs(todo): drift-cleanup audit — close stale unchecked items, collapse duplicate block

Closed N stale unchecked items in tasks/todo.md with file:line / PR / commit /
memory citations per the audit. Operator-gated and evidence-gated items moved
to explicit "Outstanding (live, NOT closed)" section. Duplicate content
(lines 248-519 of pre-edit file) collapsed — strict subset of head copy.

Audit findings for BL-NEW-BL060-CYCLE-VERIFY, BL-NEW-REVIVAL-VERDICT-WATCHDOG,
BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG, BL-NEW-SCORER-DEAD-SIGNAL-COMMENT-
CONVENTION captured as findings-only (no code change) per operator scope.

backlog.md verified for the 10 named entries; no status drift detected — no
edit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Push the worktree branch**

```bash
git push -u origin worktree-overnight-drift-cleanup
```

---

## Task 7: Open PR + dispatch 3 reviewers

**Files:** GitHub PR.

- [ ] **Step 1: Open PR via gh**

```bash
gh pr create --title "docs(todo): drift-cleanup audit — close stale items, collapse duplicate block" --body "$(cat <<'EOF'
## Summary

- Closed N stale unchecked items in `tasks/todo.md` with file:line / PR / commit / memory citations.
- Collapsed duplicate content block (pre-edit lines 248-519, strict subset of lines 1-247).
- Preserved operator-gated, evidence-gated, and date-gated items in new "Outstanding (live)" section.
- Findings-only audit notes for 4 BL-NEW entries; no code change per operator scope.
- `backlog.md` verified for 10 named entries; no status drift detected.

## Scope guardrails honored (per overnight assignment)

- No code, no schema, no scripts, no settings.
- No live config or secrets touched.
- No backlog status flips without commit / file:line evidence.
- No work on evidence/date-gated items (#158 24h validation, /coins/{id} fallback, stale-count alert baseline, June-gated decisions, HPF n≥20, first_signal 2026-05-31, Minara D+14).

## Test plan

- [ ] `awk 'END{print NR}' tasks/todo.md` shows 200-300 lines (was 519).
- [ ] `grep -c '^- \[x\]' tasks/todo.md` and `grep -c '^- \[ \]' tasks/todo.md` distinguish closed vs live unchecked.
- [ ] Spot-check each "Outstanding (live)" item has a clear gate condition (operator-gated / evidence-gated / date-gated).
- [ ] Spot-check each "Closed this audit" item has a citation that re-resolves to in-tree evidence.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Capture PR URL + number for the 3-reviewer dispatch step (Task 8 below)**

---

## Task 8: 3 parallel PR reviewers (orthogonal vectors)

**Files:** No file edit. Agent dispatch only.

Dispatch 3 reviewers via the Agent tool in a single message with three independent tool calls. Each gets a different attack vector — converging on the same axis produces consensus and misses orthogonal bugs (per CLAUDE.md §8).

- [ ] **Step 1: Reviewer A — evidence-citation rigor**

Subagent type: `pr-review-toolkit:code-reviewer`. Prompt focus: for each `[x]` closure in `tasks/todo.md`, does the citation actually re-resolve to in-tree evidence (the PR # / commit SHA / file:line / memory file referenced)? Flag any citation that is fabricated, stale, or imprecise.

- [ ] **Step 2: Reviewer B — scope creep / blast-radius**

Subagent type: `superpowers:code-reviewer`. Prompt focus: did the PR stay within operator's scope (docs-only, no code/schema/scripts/settings, no live config or secrets, no evidence/date-gated item touched)? Flag any deviation. Check the duplicate-block collapse did not silently delete genuinely-unique content.

- [ ] **Step 3 v2: Reviewer C — structural / Markdown integrity (R-B N1 fold)**

Subagent type: `pr-review-toolkit:code-reviewer` (re-purposed for structural sweep). Prompt focus: does the cleaned `tasks/todo.md` (a) parse as valid Markdown end-to-end, (b) preserve every operator-pastable SQL block and runbook command verbatim from the pre-edit file, (c) maintain a clean `##` heading tree without orphaned content under merged sections, and (d) keep the `## Historical (pre-cycle-14)` section intact and accessible? This axis is orthogonal to Reviewer A (citation truthfulness) and Reviewer B (scope discipline) — neither catches a structurally-broken Markdown table or a silently-truncated SQL block.

- [ ] **Step 4: Fold reviewer findings**

For each CRITICAL or MUST-FIX, edit `tasks/todo.md` to apply the fix. Commit as separate commit; do not amend. For NICE-TO-HAVE / NIT, document in PR comment + defer.

---

## Task 9: Memory update + lessons capture

**Files:** memory MEMORY.md, optional `tasks/lessons.md`.

- [ ] **Step 1: Append memory checkpoint**

Write `~/.claude/projects/C--projects-gecko-alpha/memory/project_drift_cleanup_2026_05_19.md` with: PR URL, summary of items closed, summary of findings-only audits, and a one-line lesson. Update MEMORY.md with a single line pointer.

- [ ] **Step 2: Optionally append lessons.md**

If the audit surfaced a new pattern (e.g., "todo.md grows duplicate blocks when cycle-N entries are prepended without removing cycle-N-5 tail"), add a one-paragraph entry to `tasks/lessons.md`. Else skip.

---

## Task 10: Priority 2-4 follow-on items (after Priority 1 PR is in review)

Operator scope lists Priority 2-4 items 3-12. Many are operator-gated or findings-first.

- [ ] **Item 3-5: BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE / CRON-DRIFT-WATCHDOG / CG-FREE-TIER-DEMO-API-KEY**

All three require live config / SKILL.md / credential changes. Operator scope: "If not authorized to mutate live config, do not fake completion. Produce an operator-ready checklist and leave status unchanged." 

The runbooks already exist (`tasks/runbook_operator_alert_activation_2026_05_19.md`, `cron/README.md`, `tasks/runbook_cg_demo_api_key_2026_05_18.md`). No new artifact needed.

Action: do NOT execute. Leave backlog status unchanged. Document in PR comment that operator-execution gates remain.

- [ ] **Item 6: BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-OPERATOR-PREFERENCE**

Findings doc already shipped (`tasks/findings_social_mentions_denominator_audit_2026_05_17.md`); Variant B vs C decision is operator's. No new memo needed unless audit findings reveal staleness.

- [ ] **Item 7: Provider enablement guardrails (Moralis + Helius)**

Audit findings already documented (`tasks/findings_moralis_plan_audit_2026_05_18.md`, `tasks/findings_helius_plan_audit_2026_05_18.md`). Runbooks may be skeletal — check and file docs follow-up only if gaps exist.

- [ ] **Item 8: BL-NEW-BL060-CYCLE-VERIFY**

Task 5 Step 1 above covers the drift-check. Findings-only outcome.

- [ ] **Item 9: BL-NEW-REVIVAL-VERDICT-WATCHDOG**

Task 5 Step 2 above covers the drift-check. Findings-only outcome. Operator scope: "Build only after plan + review because this touches trading decision hygiene." Defer build to a separate session.

- [ ] **Item 10: BL-NEW-SCORER-DEAD-SIGNAL-COMMENT-CONVENTION**

Task 5 Step 4 above covers the drift-check. If convention is in-tree, CLOSE. Else defer.

- [ ] **Item 11: BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG**

Task 5 Step 3 above covers the drift-check. Findings-only outcome.

- [ ] **Item 12: Open PR rebase triage (#105, #34, #33)**

Per memory `project_stale_pr_triage_2026_05_18.md`, all three already triaged and commented on 2026-05-18. No action needed unless a new audit surfaces drift.

---

## Self-Review (pre-execution)

**1. Spec coverage:** Operator scope items 1-12 mapped to Tasks 1-10 above? Yes — Priority 1 → Tasks 2-9; Priority 2-4 → Task 10.

**2. Placeholder scan:** Any "TBD" / "implement later" / "fill in details"? Each Task 2 step has a concrete grep / read command; each Task 3 step has a concrete Edit shape; each Task 4 step has a concrete status verification check.

**3. Scope-guardrail consistency:** Every Task explicitly avoids code, schema, scripts, settings, live config, secrets, and evidence/date-gated items. Yes.

**4. Reversibility:** Pure docs PR. Revertible via single `git revert` if 3-reviewer pass surfaces a CRITICAL.

---

## Execution Handoff

This plan will be executed inline in this session via `superpowers:executing-plans`. No subagent-driven dispatch needed for execution — the plan is small enough to keep in main context. Subagent dispatch happens only at Task 8 (3 parallel PR reviewers).
