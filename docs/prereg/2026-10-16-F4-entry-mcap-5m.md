**New primitives introduced:** NONE (pre-registration record).

# F4 entry-mcap split at $5M (forward pre-registration, 2026-10-16)

## Why

Data review finding 3 (`docs/analysis/2026-10-16-data-review.md`): in the
2026-09-18 long-hold study, F4 coins with entry market cap of $5M or more read
n=46, mean 3.54x, median 0.86x, while F4 coins under $5M read n=52, mean
0.54x. That split was found after the fact, on the same data. It is
in-sample and post-hoc, so it proves nothing. This record fixes the test
before any forward data exists.

## Rule

- Cohort: F4 watchlist entrants (`watchlist.rule_id = 'F4'`) admitted by the
  live `rank` job from activation onward.
- Split: `json_extract(features_at_entry_json, '$.mcap_usd') >= 5000000` is
  the `>= $5M` side; a numeric value below that is the `< $5M` side. A NULL or
  non-numeric entry mcap is excluded from both sides and counted as
  `mcap unknown`.
- No admission change. The split does not alter F4 admission, ranking, the
  watchlist cap or exits. It is a reporting cut only.
- Forward-only. No backfill, no replay of history, no re-use of the
  2026-09-18 study coins as evidence for this test.

## Hypothesis

The `>= $5M` side's 90 d mean multiple vs BTC exceeds the `< $5M` side's.

## Measurement

The weekly `scorecard` job prints, under the F4 block, one line per horizon
for each side: `F4 mcap≥5M <h>d` and `F4 mcap<5M <h>d`. A side's n is its
count of 90 d rows measured by the ledger with a usable BTC pair. The
multiple is `m = 1 + ret`; the multiple vs BTC is
`(1 + ret) / (btc_price / btc_entry)`. A total loss counts as m = 0.

## Pass criterion (all required, evaluated at the 90 d horizon)

1. n >= 20 on each side.
2. Mean ratio: (`>= $5M` mean multiple vs BTC) / (`< $5M` mean multiple vs
   BTC) >= 1.5, computed on raw values, not the printed two decimals.
3. Median multiple (`median` on the scorecard line) on the `>= $5M` side
   >= 1.0.

Until both sides reach n >= 20 at 90 d the verdict is UNDERPOWERED, not FAIL.
The thresholds above are not tuned after any forward data is seen. A change
to the split, the criterion or the horizon is a new pre-registration with its
own hash.

## Integrity

Amended 2026-10-16, before any forward data existed: criterion 2 now states
that the ratio is computed on raw values. Wording only; no threshold, split or
horizon changed.

The footer line holds the SHA-256 of every byte above it, footer excluded.
Reproduce with:

    sed '$d' docs/prereg/2026-10-16-F4-entry-mcap-5m.md | sha256sum

sha256(body above): 8804cdd7d5507e866a1ba94da45e35f9dff6b7c0af0dafe62fe3f2b2245ecf79
