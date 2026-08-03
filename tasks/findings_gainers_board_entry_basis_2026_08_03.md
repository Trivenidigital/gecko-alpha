# Trade-inbox board surfaces already-run tokens as "fresh" — entry-basis defect

**Date:** 2026-08-03
**Status:** ANALYSIS COMPLETE — no code changed. One fix recommended, one question to
settle first, one policy call for the operator.
**Trigger:** operator observed the board promoting WOOF (Shibinhood) to "Best watch"
while it showed **+371.92% 24h** — "system is finding signal after they raised, not
before."

---

## Verdict in one line

**Detection was not late. The board's lateness guards are inert**, because
`pct_from_entry` is computed against a near-current price while `opened_at` reports
the first sighting — so nothing can ever be classified "already ran."

---

## 1. WOOF was caught early

| Moment | 24h change | mcap |
|---|---|---|
| First tracked `2026-08-02T13:18:14` | **+21.3%** | $827,484 |
| +73 min | +105.4% | $1,336,233 |
| Screenshot (operator) | +371.92% | $1.4M |
| Peak observed | +494.9% | — |

The tracker admitted WOOF at +21.3%, effectively as early as its own gate permits
(see §4). The complaint is real but it is about **surfacing**, not detection.

## 2. The defect — `opened_at` and `entry_price` come from different times

Live `/api/trade_inbox` for WOOF:

```
opened_at        = 2026-08-02T13:18:14.300067+00:00   <- exactly the first snapshot
current_price    = 1.41e-06
pct_from_entry   = -2.76
price_change_24h = 372.93
entry_quality    = acceptable_pullback
window_state     = open
trade_score      = 38.0   (top of Best Watch)
```

But the stored price **at that timestamp** was `8.27484e-07`:

```
(1.41e-06 - 8.27484e-07) / 8.27484e-07 = +70.4%
```

So the true move since the entry the row itself names is **+70.4%**, and the board
reports **−2.76%**. `entry_price` is implicitly ~current (back-solves to ≈1.45e-06,
a price from within the hour), not the price at `opened_at`.

## 3. Why that disables every lateness guard

All three gates key **exclusively** off `pct_from_entry`:

| Guard | Location | Thresholds |
|---|---|---|
| `_trade_window_state` | `dashboard/db.py:1225-1237` | `<-10` closed · `<=8` open · `<=25` closing · else late |
| `entry_quality` | `dashboard/db.py:1583-1592` | `already_faded` / `fresh_entry` / `acceptable_pullback` / `already_ran` |
| `_trade_block_reason` | `dashboard/db.py:1240+` | — |

Pin `pct_from_entry` near zero and `already_ran` / `late` / `closed` become
unreachable on the tracker path.

**Signature in the live payload:** all 28 board rows sit between **−0.16% and
−6.65%**, while their 24h moves span **−23.7% to +372.9%**. Prices that dispersed
cannot produce entry deltas that tight unless entry ≈ now.

**Corrected against first-sight price, across all 211 tracked coins:**

| Would actually be | n |
|---|---|
| LATE — already ran >25% | 16 |
| CLOSING (8–25%) | 20 |
| Genuinely open | 70 |
| CLOSED — faded >10% | 104 |

**120 of 211 (57%)** are shown as fresh/acceptable when already run or already dead.
Worst current offenders: `what-if-3` +254.9%, `stonkbroker` +179.9%, `bless-2`
+116.0%, `aeon-2` +106.3%, `shibinhood` +76.4%.

## 4. Run-up is rewarded, never penalized

```python
# dashboard/db.py:1287
momentum = row.get("price_change_24h")
if momentum is not None and momentum > 0:
    score += min(10, momentum / 5)
```

Saturates at +50%. **+371% scores the same +10 as +50%**, with no penalty above.
`momentum_24h_positive` (`dashboard/db.py:1341`) then prints on the card as a reason
to look. `price_change_24h` is used *only* as a positive credit — it is never a
ceiling anywhere in the board.

## 5. One genuine structural ceiling (working as designed)

`GAINERS_MIN_CHANGE = 20.0` — `scout/config.py:527`.

Empirically the minimum `price_change_24h` at first sighting across all 211 coins is
**exactly 20.0**. Nothing has ever entered this lane below +20%. **No scoring change
can make this lane earlier than +20%.**

Distribution at first sighting: 124 in 20–25% · 57 in 25–50% · 18 in 50–100% ·
4 in 100–300% · 8 above 300%. So the typical catch is +20–25% (reasonable), with a
5.7% genuinely-late tail.

---

## Recommended fix (not implemented)

Compute `pct_from_entry` from `gainers_snapshots.price_at_snapshot` at the row's
first sighting. The value is already stored and `opened_at` already points at the
right row. This one change activates three guards that exist, are correct, and are
currently inert. WOOF flips to `already_ran` / `late` and takes the −35 window
penalty instead of ranking first.

**Settle this BEFORE implementing:** some rows show first-sight price *exactly*
equal to current — `buy-and-retire-3` (0.0% since first sight but +22,669% 24h) and
`slop-3` (0.0% but +378.9%). That is consistent with either (a) genuinely just
added, or (b) `price_at_snapshot` being backfilled with a current price on some
write path. If (b), it is not a trustworthy entry basis and the fix must source
entry elsewhere. Check the writer at `scout/gainers/tracker.py:74`.

**Operator policy call, separate from the bug:** whether the board should also carry
a hard 24h ceiling. The paper lane already has one (`PAPER_GAINERS_MAX_24H_PCT =
50.0`, `scout/config.py:1102`); the board has none. That is a judgement about desk
behaviour, not a defect.

---

## Exact repro (don't redo the analysis)

```sql
-- WOOF trajectory
SELECT snapshot_at, price_at_snapshot, market_cap, ROUND(price_change_24h,1)
FROM gainers_snapshots WHERE coin_id='shibinhood' ORDER BY snapshot_at LIMIT 3;

-- true move since first sight vs what the board claims
WITH f AS (SELECT coin_id, MIN(snapshot_at) t0 FROM gainers_snapshots GROUP BY coin_id)
SELECT g.coin_id,
       ROUND((p.current_price-g.price_at_snapshot)/NULLIF(g.price_at_snapshot,0)*100,1)
         AS true_pct_since_first_sight,
       ROUND(g.price_change_24h,1) AS chg24_at_first_sight,
       ROUND(p.price_change_24h,1) AS chg24_now
FROM gainers_snapshots g
JOIN f ON g.coin_id=f.coin_id AND g.snapshot_at=f.t0
JOIN price_cache p ON p.coin_id=g.coin_id
WHERE true_pct_since_first_sight > 25
ORDER BY true_pct_since_first_sight DESC;

-- earliness ceiling: min is exactly 20.0
WITH f AS (SELECT coin_id, MIN(snapshot_at) t0 FROM gainers_snapshots GROUP BY coin_id)
SELECT MIN(g.price_change_24h), COUNT(*) FROM gainers_snapshots g
JOIN f ON g.coin_id=f.coin_id AND g.snapshot_at=f.t0;
```

Live board: `curl -s http://127.0.0.1:8000/api/trade_inbox` on srilu-vps.

---

## Two wrong turns — do not repeat them

Both cost a full analysis round and both produced *plausible, quotable* numbers.

1. **`candidates.market_cap_usd` is mutable.** `upsert_candidate`
   (`scout/db.py:8280-8310`) sets `{col}=excluded.{col}` for **every** column except
   `contract_address` and `first_seen_at`. So the mcap on a candidates row is the
   value at its **last** re-ingest, not at first sighting. Any "we saw it at $X"
   claim built on that column is invalid. Use append-only `gainers_snapshots`
   (`snapshot_at` + `price_at_snapshot`) for point-in-time facts.

2. **`token_name` is not an identity key.** Joining the address-sourced row to the
   CG-slug row on name produced apparent 2082x and 1135x pre-move catches for
   `Soon` and `TROLL`. Both are false: the CG slugs are `soon-2` / `troll-2` — the
   `-2` suffix is CoinGecko's marker for a *different* token sharing a symbol. Only
   `Shibinhood` survived scrutiny, and only because the name is distinctive.
   Cross-source identity needs the `/coins/{id}.platforms` hop (see memory
   `feedback_cg_slug_not_address_for_cg_sourced_rows`).

Related: this is the §9c lever-vs-data-path shape — `price_change_24h` is displayed
prominently and looks like the control, while `pct_from_entry` is the actual gate.
And per `feedback_evidence_that_does_not_discriminate`: the check that settled it was
asking what the payload would read *if* entry were measured from first sight
(+70.4%), then observing that it reads −2.76%.
