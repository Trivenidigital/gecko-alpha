**New primitives introduced:** `scripts/audit_social_mentions_denominator.py` read-only evidence script.

# Social Mentions Denominator Evidence Refresh - 2026-06-01

**Source:** srilu `/root/gecko-alpha/scout.db`, read-only audit run 2026-06-01T17:51:18Z from `/tmp/audit_social_mentions_denominator_20260601.py`.

**Scope:** Evidence refresh only. No scorer, gate, config, ingestion, or runtime behavior changed.

## TL;DR

`social_mentions_24h` remains structurally dead. In the live DB, all 1,714 `candidates` rows have `social_mentions_24h = 0`; Signal 5 would fire for 0 rows. The social scoring destination tables are still empty (`social_signals`, `social_baselines`, `social_credit_ledger` all 0).

The fresh closed-form denominator read still supports the prior Option B recommendation:

- Variant B, remove social denominator and recalibrate gates 60->65 / 70->75: closed-form 0 min-score flips and 0 conviction-threshold flips across 3,967,523 `score_history` rows.
- Variant C, remove social denominator without recalibrating gates: closed-form 38 row-level min-score promotions, collapsing to 6 distinct contracts, with 0 matching `paper_trades` in that approximate promoted set.
- No variant pushes any row through conviction 70; current max score is 59.0.

## Live Evidence

| Check | Result |
|---|---:|
| `candidates` rows | 1,714 |
| `social_mentions_24h > 50` | 0 |
| `social_mentions_24h > 0` | 0 |
| max `social_mentions_24h` | 0 |
| `score_history` rows | 3,967,523 |
| max `score_history.score` | 59.0 |
| rows `score >= 60` | 0 |
| rows `score >= 70` | 0 |

## Variant Read

### Variant B - remove + recalibrate gates

Closed-form approximation from stored final scores, using `SCORER_MAX_RAW=208` and `max_raw_without_social=193`. `score_history` does not persist raw points or fired signal lists, so this is a boundary estimate, not an exact re-score.

| Boundary | Promotions | Demotions |
|---|---:|---:|
| min-score 60->65 | 0 | 0 |
| conviction 70->75 | 0 | 0 |

This is still the conservative recommendation because it removes the phantom denominator without widening the current gate surface.

### Variant C - remove only

| Boundary | Newly pass |
|---|---:|
| min-score 60 | 38 score-history rows |
| conviction 70 | 0 score-history rows |

Paper-trade cross-check for Variant-C min-score promotions:

| Check | Result |
|---|---:|
| distinct promoted contracts | 6 |
| promoted contracts with paper trades | 0 |
| total PnL observed | unavailable |
| win pct observed | unavailable |

Variant C remains viable only if the operator explicitly wants a small recall-widening change. The current evidence does not show missed profitable paper trades.

## Bridge Readiness

| Bridge | Fresh result | Verdict |
|---|---:|---|
| `narrative_alerts_inbound` 7d rows | 389 total, 0 resolved | Not ready for scorer replacement |
| `tg_social_messages` 24h contracts | 5 distinct contracts, 11 messages, 0 invalid JSON rows | Not ready for scorer replacement |
| `social_signals` | 0 rows | Not wired |
| `social_baselines` | 0 rows | Not wired |
| `social_credit_ledger` | 0 rows | Not wired |

## Re-run

```bash
python scripts/audit_social_mentions_denominator.py --db scout.db --pretty
```

The script opens SQLite with `mode=ro` and `PRAGMA query_only=ON`, exits 2 on required-schema drift, and emits a JSON report for future B/C evidence refreshes.

## Decision State

Recommendation remains **Option B**: remove Signal 5 from the scorer denominator and recalibrate gates 60->65 / 70->75.

Behavior change remains deferred to the existing B/C operator-decision rows. This refresh makes the evidence re-runnable; it does not itself change scoring.
