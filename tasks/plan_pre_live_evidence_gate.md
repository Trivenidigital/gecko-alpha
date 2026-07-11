**New primitives introduced:** NONE (criteria doc)

# Pre-Live Evidence Gate — Pre-Registered Promotion Criteria (LIVE-06 + LIVE-12)

**Status:** criteria doc. No code. No flag flips. Defines the pre-registered, data-bound
criteria that MUST all pass before ANY live-trading flag (`LIVE_TRADING_ENABLED`,
`LIVE_USE_ROUTING_LAYER`, `LIVE_MODE=live`) is flipped. Pre-registration is the point:
these thresholds are fixed here, in advance, so a later session cannot rationalize a
flip against post-hoc-chosen numbers.

**Closes:** LIVE-06 (this doc) + LIVE-12 (the exit-policy comparability analysis + decision,
§5 and Appendix B below). Backlog: `tasks/backlog_fable_analysis_2026_07_10.md`
(LIVE lane, LIVE-06/LIVE-12).

**Governing precedent, read first:** the standing **LIVE-ENABLE GATE** (operator, 2026-07-06,
`tasks/findings_live_trading_m1_audit_2026_07_06.md` header; project memory
`feedback_live_enable_gate_2026_07_06.md`). That gate — the four merged S1 fix-PR numbers
must be cited in any enable request or the request is refused — sits ON TOP of everything
here (Criterion 6). This doc adds the *evidence* preconditions beneath it; it does not
weaken or replace it.

---

## Hermes-first analysis

Per `docs/gecko-alpha-alignment.md` §"Hermes-first analysis convention" (2026-05-04):
mandatory section, checked against the Hermes skill hub
(`hermes-agent.nousresearch.com/docs/skills`) + awesome-hermes-agent ecosystem.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Statistical validation (bootstrap CI, regime split, slippage sensitivity) | none found — Hermes skills are agent-capability primitives (tools/actions), not statistical-inference libraries | Build from existing in-tree battery: `scripts/backtest_high_peak_existing_data_battery.py` already implements bootstrap-CI + cohort-widen + regime-split + slippage sweep (§11b precedent). Reuse, do not re-import. |
| Trading-evidence / promotion-gate framework | none found — no Hermes skill governs "promote a paper strategy to live capital" | Build from scratch (project-specific): the gate is defined against this project's own `shadow_trades`/`paper_trades` schemas and the standing live-enable gate. Not a general capability. |
| SQL / observability query harness | none found — data lives in a local SQLite (`scout.db`); querying is `sqlite3`, not an agent skill | Use in-tree `sqlite3` two-step over SSH (per global CLAUDE.md platform constraint). No external tool. |

**awesome-hermes-agent ecosystem check:** scanned for trading-strategy-promotion,
statistical-gate, and paper-to-live-transition skills — none apply. **Verdict:** this is a
pure documentation/criteria artifact over project-internal data; the only reusable
building block is the *already-in-tree* existing-data battery script. No Hermes primitive,
no new custom code.

---

## 1. Context — why a gate, and why now

- **The evidence base was silently frozen 33 days.** kill_events #1 (`daily_loss_cap`,
  `daily_sum=-50.52 cap=-50`) fired 2026-06-06T04:31:18Z with `killed_until`
  2026-06-07T00:00:00Z but never cleared — `auto_clear_if_expired()` had zero production
  callers, so `is_active()` latched it forever and Gate 1 rejected every shadow open. The
  LIVE-01 fix auto-cleared it **2026-07-10T21:42:41Z** (`cleared_by=auto_expired`), verified
  in prod. **This timestamp is the post-unfreeze boundary used by every criterion below.**
- **The pre-freeze shadow ledger is sparse and non-representative.** 133 shadow rows total;
  109 rejected, **24 closed** (17 duration / 5 SL / 2 TP), all dead 2026-06-04→. All 24 are
  `signal_type='first_signal'` — zero coverage of any other signal. The flat TP/SL thresholds
  even *drifted mid-soak* (prod `.env` carries duplicate keys: `PAPER_SL_PCT` 20→25,
  `PAPER_MAX_DURATION_HOURS` 24→168), so the 24 are not a single-policy sample.
- **The would_be_live paper cohort is NOT a substitute.** It measures the paper *cascade*
  exit policy, not the live *flat TP/SL* policy the shadow ledger and live engine use — see
  §5 (LIVE-12). Its all-time +$1,022/96 is a different-exit-policy number.

Consequence: promotion evidence must come from **post-unfreeze shadow closes** (flat TP/SL,
the live policy), accumulated to a pre-registered `n`, and pass distribution-free tests. The
paper cohort is inadmissible as primary evidence.

**Data-bound, not calendar-bound (global §11a).** Every criterion is stated as an `n`-of-fires
threshold, not "wait Y days." At the pre-freeze observed shadow *close* rate (~24 closes over
~43 days ≈ 0.56/day, and note ~4.5 rejected per close under the universe filter), N=30 new
closes is a multi-week accumulation — but the gate is the count, not the weeks. Halt and
evaluate the moment the count is met (§11c); do not flip early on calendar, do not stall on
calendar if `n` is short.

**Current standing (2026-07-11, at authoring):** post-unfreeze closed shadow trades = **0**
(soak unfroze 2026-07-10T21:42Z, ~2.5h before the last DB snapshot; newest shadow row is a
06-04 `rejected`). Every criterion below currently evaluates to INSUFFICIENT_DATA / FAIL. This
is expected: the gate is armed, not met.

---

## 2. The six criteria (ALL must pass; each = one evaluable query)

Constant used below: `@UNFREEZE = '2026-07-10T21:42:41'` (kill_events #1 `cleared_at`). A
"new closed shadow trade" is one **opened after unfreeze AND reached a terminal `closed_*`
status** — `created_at > @UNFREEZE AND status LIKE 'closed_%'`. Opened-after-unfreeze matters:
the entry must reflect the unfrozen Gate 1, not a pre-freeze open that closed late.

### Criterion 1 — Shadow soak resumed to N ≥ 30 new closed shadow trades

Rationale: re-establish a live-exit-policy evidence base of sufficient size for
distribution-free inference (§11a). N=30 is the pre-registered floor; it is the `n` the
subsequent criteria consume.

```sql
-- C1: PASS when new_closed_shadow >= 30
SELECT COUNT(*) AS new_closed_shadow
FROM shadow_trades
WHERE status LIKE 'closed_%'
  AND created_at > '2026-07-10T21:42:41';
```

Secondary health check (soak is actually *flowing*, not re-frozen): newest shadow row < 24h
old — the LIVE-01 §12a `shadow_soak_frozen` watchdog covers this at runtime; the gate re-checks
it at evaluation time.

```sql
-- C1-health: PASS when hours_since_newest < 24
SELECT ROUND((julianday('now') - julianday(MAX(created_at))) * 24, 1) AS hours_since_newest
FROM shadow_trades;
```

### Criterion 2 — Bootstrap-CI lower bound > 0 on post-unfreeze shadow PnL

Rationale: distribution-free proof the edge is real at N≥30 without normality assumptions
(§11b analysis battery). The SQL extracts the per-trade PnL vector; the bootstrap (10,000
resamples, 2.5% lower bound) is run by the **existing** battery
`scripts/backtest_high_peak_existing_data_battery.py` — reuse, do not rebuild (Hermes-first
above).

```sql
-- C2 input extract: per-trade realized USD PnL vector (feed to bootstrap_ci, resamples=10000)
-- PASS when the 2.5% percentile of the resampled MEAN > 0
SELECT CAST(realized_pnl_usd AS REAL) AS pnl_usd
FROM shadow_trades
WHERE status LIKE 'closed_%'
  AND created_at > '2026-07-10T21:42:41';
```

### Criterion 3 — Regime-split survival

Rationale: the edge must survive a hostile regime, not just ride a benign one. Precedent: the
documented June regime was −$32.5/trade vs April −$1.1 (DASH-07 / SIG-09). Split the
post-unfreeze window on its natural regime boundary and require the mean stay positive in the
adverse cell (or, if the window spans only one regime cell, this criterion is
INSUFFICIENT_DATA and the soak extends until it spans two).

```sql
-- C3: PASS when avg_pnl_usd > 0 in EVERY regime cell with n >= 8 (n-gate against noise cells)
SELECT
  strftime('%Y-%m', created_at)                        AS regime_month,
  COUNT(*)                                              AS n,
  ROUND(AVG(CAST(realized_pnl_usd AS REAL)), 2)        AS avg_pnl_usd,
  ROUND(SUM(CAST(realized_pnl_usd AS REAL)), 2)        AS total_pnl_usd
FROM shadow_trades
WHERE status LIKE 'closed_%'
  AND created_at > '2026-07-10T21:42:41'
GROUP BY regime_month
ORDER BY regime_month;
```

### Criterion 4 — Slippage sensitivity to 500 bps

Rationale: paper→live execution risk is closed if the edge survives a plausible adverse
slippage shock (§11b sensitivity precedent; the M1 audit measured realized stop fills at
−28% vs −10% configured, so slippage is the dominant paper→live risk). Shadow already books
realistic VWAP exits via `walk_bids` + `entry_slippage_bps`; C4 adds a **further** 500 bps
(5.0 pp) round-trip adverse haircut per trade and requires the aggregate stay positive.

```sql
-- C4: PASS when total_pnl_usd_minus_500bps > 0
--   (edge survives an extra 500 bps adverse round-trip on every trade)
SELECT
  ROUND(AVG(CAST(realized_pnl_pct AS REAL)), 3)                                   AS base_mean_pct,
  ROUND(AVG(CAST(realized_pnl_pct AS REAL)) - 5.0, 3)                             AS mean_pct_minus_500bps,
  ROUND(SUM(CAST(size_usd AS REAL) * (CAST(realized_pnl_pct AS REAL) - 5.0)/100.0), 2)
                                                                                  AS total_pnl_usd_minus_500bps
FROM shadow_trades
WHERE status LIKE 'closed_%'
  AND created_at > '2026-07-10T21:42:41';
```

### Criterion 5 — Exit-policy comparability adjustment (LIVE-12)

Rationale + the measured wedge live in §5 below. The pre-registered rule this criterion
enforces: **the gate is evaluated on the shadow ledger (flat TP/SL) ONLY. The paper
would_be_live cohort is inadmissible as primary evidence, and no scalar haircut converts one
to the other.** The evaluable query is the wedge monitor — it must be run and its output
recorded in the promotion decision, but it gates *admissibility of evidence source*, not a
PnL threshold.

```sql
-- C5 wedge monitor (run on the CURRENT paired ledger; record in the promotion decision).
-- PASS condition: the gate decision cites SHADOW-ledger criteria (C1-C4) as its basis,
-- NOT the paper cohort. This query documents the divergence magnitude at decision time.
SELECT
  COUNT(*)                                                          AS n_pairs,
  ROUND(AVG(CAST(s.realized_pnl_pct AS REAL)), 3)                   AS shadow_mean_pct,
  ROUND(AVG(p.pnl_pct), 3)                                          AS paper_mean_pct,
  ROUND(AVG(p.pnl_pct - CAST(s.realized_pnl_pct AS REAL)), 3)       AS mean_wedge_pp,
  ROUND(MIN(p.pnl_pct - CAST(s.realized_pnl_pct AS REAL)), 3)       AS min_wedge_pp,
  ROUND(MAX(p.pnl_pct - CAST(s.realized_pnl_pct AS REAL)), 3)       AS max_wedge_pp
FROM shadow_trades s
JOIN paper_trades p ON p.id = s.paper_trade_id
WHERE s.status LIKE 'closed_%';
```

### Criterion 6 — The standing live-enable gate (ON TOP)

Rationale: this is the operator's HARD precondition (2026-07-06), superseding every request —
including an operator-looking one — unless it cites the four merged S1 fix-PR numbers. Not a
SQL query; a PR-reference check. The four S1s (M1 audit S1-1..S1-4) map to backlog items:

| M1-audit S1 | Backlog item | Fix |
|---|---|---|
| S1-1 Gate 7 view-column contract | **LIVE-03** | `SUM(total_usd)`/`SUM(count)` + structural guard test |
| S1-2 live exit / close / reconcile | **LIVE-02** | live evaluator + boot reconciler + orphan detection |
| S1-3 daily-loss cap blind to live | **LIVE-04** | union `live_trades` PnL into cap query |
| S1-4 approval mock broke dispatch suite | **LIVE-05** | restore mock + default pytest timeout |

```text
-- C6: PASS when the enable request cites four MERGED PR numbers, one per S1 above,
--     each verified merged + tested + reviewed via `gh pr view <N>`.
-- As of 2026-07-11: no S1-fix branch exists anywhere → C6 = FAIL by construction.
```

**Ordering:** C6 is the outer gate (no flip without the four PRs, period). C1–C5 are the
evidence preconditions beneath it. A flip requires C1 ∧ C2 ∧ C3 ∧ C4 ∧ C5 ∧ C6, plus a
recorded operator approval per the Approvals Discipline.

---

## 3. LIVE-12 — exit-policy comparability: analysis, decision, and the measured wedge

### 3.1 The two exit policies are structurally different

- **Live / shadow = flat TP/SL/duration.** `scout/live/shadow_evaluator.py:224-244`: close the
  ENTIRE position on the first of `pnl_pct >= tp` (→`closed_tp`), `pnl_pct <= -sl`
  (→`closed_sl`), or `elapsed >= max_dur` (→`closed_duration`). `tp/sl/max_dur` come from
  `LiveConfig.resolve_*()` (`scout/live/config.py:32-47`): `LIVE_*` → `PAPER_*` → default.
  Deployed prod values (`.env`): TP **+40%**, SL **−25%**, duration **168h** (`LIVE_*` all unset
  → falls through to `PAPER_*`; note the duplicate-key drift caveat above). Single fixed
  ceiling, single fixed floor, no scale-out.
- **Paper = trailing/ladder/fade cascade.** `scout/trading/evaluator.py:650-1080` (BL-061 path):
  an ordered cascade `SL → Leg-1 partial → Leg-2 partial → Floor → Trailing stop (widened by
  moonshot floor / conviction-lock / low-peak) → High-peak fade → Peak-fade → Momentum-death →
  Expiry`. Partial ladder sells bank profit while a runner rides; the fixed-TP exit is
  *structurally unreachable* for ladder rows (`evaluator.py:664-668`). The right tail is
  harvested by the trailing machinery, not capped.

These produce different realized PnL on the same entry. The shadow ledger — the BL-055
evidence base — therefore measures a different exit policy than either the paper cohort or the
live engine's *own* flat policy. (This is M1-audit S2-5 / backlog LIVE-12.)

### 3.2 Decision: DOCUMENT the non-comparability (not: share the helper now)

The backlog offers two options: (a) share an exit-decision helper between paper and shadow so
they close identically, or (b) document the non-comparability and bake it into the LIVE-06
criteria. **Take (b).** Rationale:

- Sharing the helper is a **large, high-risk refactor on the money path.** The paper cascade
  threads ~15 knobs (per-signal `signal_params`, BL-067 conviction-lock overlay, moonshot
  floor `max()`, low-peak trail, high-peak-fade, peak-fade, momentum-death dry-run) and its
  exit order is locked in by regression tests (`tests/test_moonshot_exit.py`). The live path
  is *intentionally* a simpler flat policy (a first live venue should not carry the full
  cascade's surface area). Forcing them to share a helper either drags the cascade's complexity
  onto the money path or flattens the paper policy — both are worse than documenting the gap.
- **The comparison does not need them to be identical.** What the gate needs is that promotion
  evidence come from the *live* exit policy. The shadow ledger already IS the live policy
  (flat TP/SL). So the correct move is not to unify the policies — it is to (i) forbid the
  paper cohort as evidence, and (ii) evaluate the gate on the shadow ledger directly. Both are
  now criteria (C5, and C1–C4 respectively).
- The shared-helper refactor remains the eventual, *correct* fix — but it is gated behind the
  live path being real (S1-2's live evaluator must exist first) and is scoped in Appendix B, not
  built here.

### 3.3 The measured wedge (prod, 2026-07-11, `/root/gecko-alpha/scout.db`)

`shadow_trades.paper_trade_id` is an FK to `paper_trades(id)`, so the wedge is an **exact
paired comparison** — same token, same entry, same signal — not a lossy symbol join. All 24
closed shadow trades pair 1:1 with their paper-cascade parent. Comparing shadow
`realized_pnl_pct` (flat TP/SL) against paper `pnl_pct` (full blended cascade return; the
banked ladder gains are folded into `pnl_usd`/`pnl_pct` at `paper.py:677-683`, verified):

| Metric | Value |
|---|---|
| n paired closed | **24** (all `first_signal`) |
| Shadow (flat TP/SL) mean realized | **−6.29%** |
| Paper (cascade) mean realized | **−8.36%** |
| **Mean wedge (paper − shadow)** | **−2.07 pp** |
| Mean \|wedge\| | 2.89 pp |
| **Median \|wedge\|** | **0.78 pp** |
| Wedge range | **[−41.3 pp, +9.9 pp]** |
| Shadow ≥ paper | **22 / 24** |
| Cascade strictly better | **1 / 24** (superrare) |

### 3.4 The finding REFUTES the backlog's stated direction — and that strengthens the gate

The backlog premise ("shadow flat TP/SL *understates* paper-cascade PnL", implying a positive
haircut to project live *down* from paper evidence) is **empirically false on this cohort.**
The realized data shows the opposite central tendency: **the cascade underperforms flat TP/SL
by ~2 pp on the mean, and flat TP/SL is ≥ the cascade on 22 of 24 trades.** Mechanism:

- In the **body** (losers + small movers, ~22 rows), the cascade's extra machinery slightly
  *subtracts* — ladder-leg slippage plus riding losers to a marginally lower stop/expiry — so
  flat TP/SL books a touch better (median |wedge| just 0.78 pp).
- The wedge is **tail-dominated and sign-unstable.** The two extremes drive the mean and point
  in **opposite** directions:
  - `orca` (paper_id 1451): shadow locked **+40.9%** at the flat +40% TP; the cascade banked a
    leg then trailing-stopped the runner back to **−0.37%** → shadow **+41.3 pp** better
    (spike-then-reverse: flat TP wins).
  - `superrare` (paper_id 906): shadow duration-exited at **+1.8%**; the cascade rode the
    trailing stop to **+11.6%** → cascade **+9.9 pp** better (monotonic run: cascade wins).

### 3.5 The pre-registered "haircut" — stated honestly

The task asked for a haircut. The defensible answer is that **no scalar haircut/markup is
admissible**, and here is why, stated as the rule the gate enforces:

1. The measured mean wedge is **−2.07 pp (paper − shadow)** on n=24 — i.e., if one insisted on
   projecting live (flat TP/SL) *from* paper numbers, the point estimate would be a **+2 pp
   markup**, not a haircut. But that number is an artifact of one tail row (`orca`, +41 pp);
   drop it and the sign of the correction changes. A quantity that flips sign on a single trade
   is not a usable adjustment.
2. The wedge is **path-dependent** (spike-then-reverse vs monotonic run) and **tail-dominated**
   (median |wedge| 0.78 pp, but per-trade range ±40 pp). A single live trade can swing the
   realized wedge by ~40 pp. No constant scales one ledger onto the other.
3. **Cohort-narrow:** all 24 pairs are `first_signal`; the wedge is unmeasured for every other
   signal, and the flat thresholds drifted mid-soak. Extrapolating even the −2 pp point
   estimate beyond `first_signal` is unsupported.

**Therefore the "adjustment" baked into the criteria is a RULE, not a number:** promotion
evidence is drawn from the **post-unfreeze shadow ledger only** (C1–C4, which already measure
the live flat policy). The paper would_be_live cohort is **inadmissible as primary evidence.**
If the paper cohort is ever cited as a secondary sanity check, it must carry the full empirical
wedge distribution as an irreducible per-trade uncertainty band (**[−41 pp, +10 pp], n=24,
first_signal only**) — never a point adjustment — and it still cannot substitute for a
shadow-ledger PASS. C5 enforces that the recorded promotion decision cites shadow-ledger
criteria as its basis.

---

## Appendix A — LIVE-10 Binance $50 go-checklist (skeleton)

Binance is the only near-executable venue (adapter complete; ccxt unwired; no Base/Jupiter
adapter — LIVE-11 parks all three). This checklist feeds the standing live-enable-gate ask;
every box needs a query or PR proof (LIVE-10 VALIDATE). It does NOT lower the gate — it is the
operational pre-flight *after* C1–C6 pass.

- [ ] **Four S1 fix-PRs merged + tested + reviewed** (LIVE-02/03/04/05). Proof: `gh pr view <N>`
      per PR, `state=MERGED`, CI green, review approved. (= Criterion 6.)
- [ ] **Binance API key has TRADE scope** (not read-only). Proof: key-permissions probe or a
      rejected-for-scope test order; reject_reason `api_key_lacks_trade_scope` must NOT fire on
      a real order. Never commit the key.
- [ ] **`LIVE_SIGNAL_SIZES` set for the enabled signal(s).** Only `first_signal` has any shadow
      evidence (all 24 closes) — do NOT enable a signal with zero post-unfreeze shadow closes.
      Proof: `.env` `LIVE_SIGNAL_SIZES` maps every allowlisted signal; cross-check
      `LiveConfig.resolve_size_usd`.
- [ ] **Balance margin.** Account free balance ≥ Σ(enabled `LIVE_SIGNAL_SIZES`) × concurrency
      cap, with headroom for the −25% SL and fees. Proof: Gate 10 balance branch exercised in
      shadow; `LIVE_DAILY_LOSS_CAP_USD` consistent with size × expected daily fires.
- [ ] **Pin `LIVE_TP_PCT` / `LIVE_SL_PCT` / `LIVE_MAX_DURATION_HOURS` explicitly.** Stop relying
      on the drifting `PAPER_*` fallback (prod `.env` already carries duplicate `PAPER_SL_PCT`
      and `PAPER_MAX_DURATION_HOURS` keys). Proof: `LIVE_*` present and equal to the thresholds
      the shadow evidence was collected under.
- [ ] **Kill-switch auto-clear confirmed live** (LIVE-01). Proof: kill_events #1
      `cleared_at=2026-07-10T21:42:41Z cleared_by=auto_expired`; staged-freeze watchdog fires.

## Appendix B — Eventual fix: shared exit-decision helper (scoped, NOT built here)

The correct long-term resolution of LIVE-12 is a single exit-decision function both evaluators
call, so paper and shadow (and live) close on identical logic and paper-exit fixes propagate
automatically. Scoped sketch (build gated behind S1-2's live evaluator existing):

- Extract a pure `decide_exit(position_state, price, depth, params, policy) -> ExitDecision`
  where `ExitDecision ∈ {hold, close(reason), partial(leg, frac)}`.
- `policy` selects `flat` (TP/SL/duration — today's shadow) vs `cascade` (BL-061 ladder —
  today's paper). Both evaluators pass their live position row + a fresh price/depth; the
  helper owns the decision, each evaluator owns persistence (shadow: `walk_bids` VWAP +
  `shadow_trades`; paper: slippage-bps + `paper_trades`).
- Contract test (backlog LIVE-12 VALIDATE): an identical seeded position fed to both evaluators
  yields the SAME `ExitDecision` under the SAME `policy`. That is the structural guard that the
  two ledgers are comparable-by-construction, retiring C5's "different policy" caveat.
- Effort: M–L, money-path — DESIGN-FIRST, and only after LIVE-02 lands the live evaluator (no
  point unifying against a live path that cannot yet close). Until then, §3.2's documentation
  decision holds and the gate runs on the shadow ledger.

---

## Approvals log

| Action | Class | Approval record | Status |
|---|---|---|---|
| Author this criteria doc (LIVE-06) + LIVE-12 analysis | documentation | task dispatch (orchestrator) | executed |
| Read-only prod SQL over SSH (scout.db) | read-only prod query | read-only; no writes | executed |
| Any live flag flip | flag/prod-state | — | **NOT approved; gated on C1–C6 + recorded operator approval** |

No code changed. No flag flipped. This doc pre-registers the criteria; it does not assert any
of them is met (all currently INSUFFICIENT_DATA / FAIL — the soak has 0 post-unfreeze closes).
