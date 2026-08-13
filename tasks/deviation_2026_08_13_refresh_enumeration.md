# Deviation record — 2026-08-13 refresh enumeration

Durable record of the first corrected combo refresh after PR #523, the
deviation it exposed, the proven root cause, and the operator ruling that
authorized the repair on `fix/refresh-enumeration-closed-window`.

This is an audit record, not a plan. It deliberately preserves one
methodological mistake rather than rewriting it — see §5.

## 1. The observation

The first natural corrected refresh fired **2026-08-13 03:02:29Z**
(`combo_refresh_done refreshed=6 failed=0`).

Operator disposition: **`#523 FIRST CORRECTED REFRESH — DEVIATION / NOT
ACCEPTED / PRESERVE STATE`**.

### Accepted positive evidence

| Combo | Result | Verdict |
|---|---|---|
| `gainers_early` 30d | 271 / 147 / 124, WR 54.24%, stayed clear | EXACT match |
| `losers_contrarian` 30d | 3 / 1 / 2, WR 33.33%, generation preserved (`suppressed_at` 06-10, `parole_at` 08-08, remaining 0), classified `terminal_incomplete` with accounting valid 3 / invalid 0 / open 0 / spent 5 | EXACT match |

The `losers_contrarian` leg also delivered the **first real page through the
post-lock alert path**: `dispatched -> telegram delivered -> delivered-log ->
marker stamped 40µs AFTER delivery`. The #523 success branch is proven in
production.

### The deviation

`volume_spike` was **not refreshed at all**. Its row remained byte-stale with
`refresh_failures = 0` — the signature of a combo that was never enumerated,
not one whose refresh failed.

## 2. Root cause (proven)

`refresh_all()` enumerated refresh candidates by **`opened_at`**:

```sql
SELECT DISTINCT signal_combo FROM paper_trades WHERE opened_at >= ?
UNION
SELECT combo_key FROM combo_performance WHERE window = '30d' AND suppressed = 1
```

while `refresh_combo()` computes economics by **`closed_at`** under the
canonical validity predicate:

```sql
... WHERE signal_combo = ? AND <VALID_TERMINAL_OUTCOME_SQL> AND closed_at >= ?
```

Membership and economics were bound to **different columns**. A combo whose
opens age out of the window while its closes remain inside it is therefore
never re-selected, and its row freezes.

`volume_spike` at the moment of the refresh:

- `MAX(opened_at)` — **69 minutes BEFORE** the enumeration cutoff
- valid closes inside the window — **21**
- suppression state — **unsuppressed**, so the `suppressed = 1` arm did not
  rescue it either

All three conditions had to hold simultaneously, which is why the defect
survived so long unnoticed.

**This is a PRE-EXISTING defect, present on `86a7c2bc` (before #523). It is
NOT a D2 regression.** #523 did not introduce it; #523's rollout is simply the
first event that made it visible, because it was the first refresh whose
per-combo results were being checked against pre-computed expectations.

### Census

**Seven unsuppressed 30d rows** currently carry stale economics. Some have been
stale **since May 2026**. The suppressed rows were never affected — the
`suppressed = 1` arm kept them enumerated, which is exactly why the defect
presented as an unsuppressed-only problem.

## 3. The side-effect inventory: two extra terminal-incomplete pages

Two additional terminal-incomplete pages fired for `cg_trending_rank+first_signal`
and `chain_completed`.

These were **deterministic, not classifier misbehaviour**.
`_process_retest_terminal_incomplete` scans **all** suppressed, exhausted parole
generations globally — it is not scoped to the combos refreshed on this run. The
exact predicate matched exactly **3** rows pre-refresh, and 3 pages were sent.

Recorded as an **incomplete side-effect inventory** on the reviewer's part: the
predicate is global, so the expected-page count should have been derived from
the global predicate rather than from the refreshed-combo list.

## 4. The ruling

**E3 + preserved opens arm.** `refresh_all()` must enumerate:

```
recent opens (opened_at >= cutoff)                    <- KEPT deliberately
∪ DISTINCT signal_combo with valid closes in window   <- NEW
∪ EVERY existing combo_performance key WHERE window='30d'  <- NEW
```

The invariant, stated in the function docstring:

> every persisted combo whose 7d/30d economic state can change because outcomes
> entered or aged out of the rolling window must be refreshed.

Notes on each arm:

1. **Opens arm — kept deliberately.** It is the only arm that sees a combo which
   has opened but not yet closed anything, so it protects cold start and first
   materialization. It is redundant for steady-state combos but not for new ones.
2. **Valid-closes arm.** Bound to the *same predicate* the economics use, not
   merely to closedness. Binding it to closedness alone would let the
   fabricated-$0 (`entry_fallback`) and operator-action (`closed_manual`) rows —
   which the economics deliberately exclude — manufacture membership for combos
   that have no row at all.
3. **Existing-30d-keys arm.** The only arm that can refresh a row *downward* as
   its outcomes age out. It **strictly subsumes** the former `suppressed = 1`
   arm, which was therefore removed rather than kept as a redundant clause.

### Re-acceptance is PREDICATE-bound

The replacement acceptance event is the next natural 03:00 refresh after
deployment, evaluated against **predicates**, not against frozen counts.

## 5. Preserved mistake: the date-fragile 25/5/20 acceptance constant

The original acceptance criterion for `volume_spike` was the frozen constant
**25 / 5 / 20**.

That constant was **date-fragile**. It was only valid at the 2026-08-11 replay
cutoff. The true value at the 2026-08-13 refresh was **21 / 5 / 16 @ 23.81%**,
because the 30d window had rolled forward and four outcomes had aged out.

**This is preserved here as a methodology mistake, deliberately not silently
rewritten.** Per the operator: acceptance criteria for a rolling-window
computation must be **predicate-bound** ("the row equals the canonical
recomputation at refresh time"), never **count-bound** against a constant
captured at a different timestamp. A count-bound criterion embeds the capture
date as a hidden parameter, and a rolling window guarantees it will drift.

## 6. Standing state at the time of this record

- Production frozen at `d7769580`; master parked at `1b7e346e`.
- #524 (Stage A TG shadow, dark) and #525 (alerter `raise_on_failure`) merged
  but **not deployed**.
- Telegram controls unchanged. No manual refresh, no manual SQL.
- Eventual deploy is **one** deployment carrying #524 (dark) + #525 + this
  repair. After it, no manual refresh: the next natural 03:00 refresh is the
  replacement acceptance event.
- Pre-merge requirement: a read-only replay over the entire production 30d-key
  set reporting every predicted change — `scripts/replay_refresh_enumeration.py`.

## 7. Related

- PR #523 — canonical outcome population + two-axis parole-completion gate
- PR #525 — failed alert delivery must raise, not stamp the marker
- `scripts/replay_refresh_enumeration.py` — read-only pre-merge replay
- `tests/test_trading_combo_refresh.py` — enumeration axes (a)-(d)
