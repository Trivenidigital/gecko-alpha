# Product note — High-Precision Trade-Now **Candidate** Lane (conceptual; BLOCKED on DEX soak)

**Date:** 2026-06-30
**Status:** CONCEPTUAL / DOCS-ONLY. **Do NOT implement.** Implementation is gated on the DEX-instrumentation
soak evidence (see "Unlock gate") — the gate is **non-negotiable**. Captured at operator direction during
the observe-only soak.
**Home backlog item:** `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN` (this note resolves its open
"scarcity algorithm / daily budget / corpus scope" gates).

**Framing (use throughout): "Trade-Now Candidate Lane."** It is a **high-priority manual-review alert** —
a candidate worth opening and reviewing *immediately* — **not** a "trade-now signal," "buy signal," or any
instruction to trade. The lane raises a candidate to the top of the operator's attention; the trade decision
stays fully manual.

## The gap (why this lane is needed)
The operator wants **a few important TG pings — high-priority candidates worth opening and reviewing
immediately** (manual review, never an instruction to trade). Today neither surface delivers that:
- **TG alert path is silent.** Alerts fire only at conviction ≥ 75 (needs quant ≥ 65); the ANSEM/under-gate
  work proved **nothing reaches 65** → zero TG "trade-now" pings. ANSEM (477×) and CATWIFHAT were *recorded
  but never alerted*.
- **Dashboard `act_now` is the opposite failure** — `Trade Inbox → act_now` (and the Trader Action Queue)
  is a broad, unvetted **review queue over open paper trades**, internally labelled "review," not a buy signal.

So the operator is caught between *a channel that pings nothing* and *queues that show too much*. The right
product is **not** "make `act_now` louder." It is a **separate, tiny, high-trust alert lane** whose job is to
send very few pings and make each one worth attention. This honors the existing anti-scope on
`/api/trade_inbox` (urgency/alert-intent stays out → a separate `/api/trade_alert_intent`-style surface).

## Operator decisions (2026-06-30)
| Parameter | Decision |
|---|---|
| **Daily budget** | **Hard cap 1–3 TG pings/day.** >3 makes it "another dashboard queue." |
| **Precision vs recall** | **Strongly precision over recall.** "Rare, worth opening immediately." Acceptable to miss winners to keep the list trustworthy. |
| **Mode** | **Manual trading only. No auto-trade, no auto-sizing.** |
| **Alert payload (every ping)** | token / chain / contract · entry mcap · age · score components · DEX source · why it passed · what would invalidate it · liquidity / volume / buy-pressure context · link to the dashboard detail page |
| **Surface** | **TG-first.** A later dashboard "Act Now" mirror MUST show **exactly the TG-pushed set** (not a broader queue) so TG and dashboard never disagree. |

## Acceptance criteria (when eventually built)
1. Dedicated alert lane **separate** from `/api/trade_inbox` (own endpoint + own TG callsite); `trade_inbox`
   contract firewall unchanged.
2. **Hard daily cap enforced** (≤ 3/day) with an auditable suppression/decision event per candidate (why
   sent / why withheld), `parse_mode=None` hygiene + `*_alert_dispatched`/`*_alert_delivered` logs (§12b).
3. **Precision floor enforced** at the gate (see target) — a candidate is pushed only if it clears the floor;
   "no qualifier today" is a valid, silent outcome (no filler pings).
4. Every ping carries the full decision payload above and a working dashboard-detail link.
5. **Dashboard mirror = the pushed set** (1:1), not a superset.
6. **No scoring/gate/threshold/trading-behavior change to the existing pipeline** — the lane reads existing
   evidence and decides dispatch; it does not alter how tokens are scored or gated.

## Precision target
- The precision floor is **chosen from evidence — the measured F1-cohort precision — NOT placeholder
  intuition.** **No numeric floor is asserted here**; asserting one pre-evidence would defeat the purpose of
  the unlock gate. The bar is set only once the soak yields the false-positive cost.
- Conceptual intent (not a number): **a strong majority of pings should be "worth having opened."**
- Recall is explicitly **not** a target for V1.

## Required evidence from the DEX soak (the Unlock gate — NON-NEGOTIABLE)
Implementation stays BLOCKED until **ALL** hold — no partial unlock, no exceptions:
1. DEX **measurable cohort `n ≥ 30`**,
2. **≥ 1 ran-≥10× candidate** in that cohort,
3. **F1 cohort re-run completed** with the never-listing **survivorship bound** reported,
4. **candidate volume and false-positive cost known**,
5. **precision floor chosen from that evidence — not placeholder intuition.**

Rationale: the whole reason the gate is currently unreachable-and-silent is that we lack the denominator to
recalibrate safely. The DEX instrumentation soak exists to earn exactly that denominator. See
`findings_same_asset_under_gate_cohort_30d_2026_06_28.md` + the runbook.

## Drift-check (don't reinvent — anchors)
- **`BL-NEW-TG-ALERT-QUALIFICATION-DESIGN`** — DESIGN-SHIPPED/TELEMETRY-SHIPPED; operator-action telemetry in
  PR #344. This note resolves its open daily-budget/scarcity gates and adds the DEX-soak unlock gate.
- **`BL-NEW-LIVE-DECISION-COCKPIT`** — parent archived; product target already states *"Show me 3–5 candidates
  worth a tiny live experiment, with reasons and caveats,"* and lists "**TG alert qualification after soak**"
  as a residual child gap. This lane is that child gap, now parameterized.
- **`BL-NEW-SIGNAL-TRUST-ROADMAP`** — the strategic frame (signal collector → trustable signal system).

## Hard boundary
No implementation now. No gate recalibration · no threshold change · no scoring change · no trading-alert
behavior change · no proxy scoring · no paid feed. Revisit only after the Unlock gate is met.
