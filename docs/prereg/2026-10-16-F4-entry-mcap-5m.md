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
- Activation cutoff: the first `rank` run executed without `--as-of` writes
  the kv key `activation:at` (its run time) once; it is never moved. The
  cohort is the F4 rows with `watchlist.entered_at >= activation:at`. While
  the key is absent the scorecard prints `activation not recorded` and the
  cohort is empty. Once the key exists `rank` refuses `--as-of` (exit 2), so
  no replay can add rows to the cohort; replays belong to the backtest
  harness.
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
for each side: `F4 mcap≥5M <h>d` and `F4 mcap<5M <h>d`. A side's n (`n=` on
the line) is its count of 90 d rows measured by the ledger with a return and
a usable BTC pair (`btc_entry` and `btc_price` non-zero); `ret_n=` counts the
rows with a return, BTC pair or not. The multiple is `m = 1 + ret`; the
multiple vs BTC is `(1 + ret) / (btc_price / btc_entry)`. A total loss counts
as m = 0.

Criterion 2's raw ratio is the value of this query against `gecko.db`:

    SELECT AVG(CASE WHEN mc >= 5000000 THEN v END)
         / AVG(CASE WHEN mc < 5000000 THEN v END)
    FROM (SELECT CAST(json_extract(w.features_at_entry_json, '$.mcap_usd')
                      AS REAL) AS mc,
                 (1 + f.ret) / (f.btc_price / f.btc_entry) AS v
          FROM forward_returns f JOIN watchlist w USING (token_id)
          WHERE w.rule_id = 'F4' AND f.horizon_days = 90
            AND f.measured_at IS NOT NULL AND f.ret IS NOT NULL
            AND f.btc_entry <> 0 AND f.btc_price <> 0
            AND json_type(w.features_at_entry_json, '$.mcap_usd')
                IN ('integer', 'real')
            AND w.entered_at >= (SELECT value FROM kv
                                 WHERE key = 'activation:at'));

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

Amended again 2026-10-16, before any forward data existed: the activation
cutoff mechanism and the query that yields criterion 2's raw ratio are now
stated, and the scorecard line names `n=` (usable BTC pair) and `ret_n=`
separately. No threshold, split, horizon or criterion changed.

The footer line holds the SHA-256 of every byte above it, footer excluded.
Reproduce with:

    sed '$d' docs/prereg/2026-10-16-F4-entry-mcap-5m.md | sha256sum

sha256(body above): ca8b2a295fbb3f93e7bbc3afbc3cc96c43ca194ba362fd9ec5477713803d60cc
