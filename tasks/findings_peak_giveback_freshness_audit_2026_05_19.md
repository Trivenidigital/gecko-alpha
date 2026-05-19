**New primitives introduced:** NONE. This is a read-only historical audit and proposal note; it makes no runtime behavior, schema, entry, suppression, sizing, or capital-allocation change.

# Findings: Peak-Giveback / Freshness Historical Audit - 2026-05-19

## Guardrail

This audit runs while the 24h actionability observation window is accumulating.
It must not change trade behavior. Do not suppress exploratory trades, change
entry thresholds, change sizing, or alter live/paper capital allocation until
`tasks/runbook_actionability_validation_2026_05_19.md` has been run and reviewed.

## Drift Check

Existing coverage:

- `scripts/analyze_profit_patterns.py` already segments closed paper trades by
  broad freshness and peak-giveback buckets.
- `tasks/findings_profit_patterns_2026_05_19.md` already identified post-entry
  giveback buckets above 15pp as weak and `peak_pct < 5%` / unknown peak as loss
  buckets.
- `tasks/findings_high_peak_giveback.md` already covers the exit-side high-peak
  runner problem.

Residual gap:

- No existing artifact quantified the pre-entry stale-entry failure mode: first
  observed snapshot -> best pre-entry snapshot -> actual paper entry price.
- This audit fills that gap with a read-only threshold sweep.

## Method

Script:

```bash
python3 /tmp/audit_peak_giveback_freshness.py --db scout.db --since "2026-05-01 14:06:00"
python3 /tmp/audit_peak_giveback_freshness.py --db scout.db
```

Local source:

```bash
python scripts/audit_peak_giveback_freshness.py --db scout.db --since "2026-05-01 14:06:00"
```

For each closed paper trade with a pre-entry snapshot price path, the script
computes:

- `freshness_minutes`: paper entry time minus first observed snapshot time.
- `pre_entry_peak_gain_pct`: best pre-entry snapshot price versus first observed price.
- `entry_gain_from_first_pct`: paper entry price versus first observed price.
- `pre_entry_giveback_pp`: `pre_entry_peak_gain_pct - entry_gain_from_first_pct`, floored at zero.
- `pre_entry_giveback_ratio`: giveback as a fraction of the pre-entry peak gain.

Price sources used:

- `gainers_snapshots.price_at_snapshot`
- `losers_snapshots.price_at_snapshot`
- `volume_history_cg.price`
- `volume_spikes.price`
- `momentum_7d.current_price`

## Coverage

Current regime, closed paper trades opened since `2026-05-01 14:06:00`:

- Closed trades: 544.
- Trades with usable pre-entry snapshot path: 340.
- Missing pre-entry path: 204.
- Analyzed cohort: +$490.68 net, +$1.44/trade, 60.9% win.
- Median freshness: 70.8h.
- Median pre-entry giveback: 8.7pp.

All closed trades:

- Closed trades: 1,409.
- Trades with usable pre-entry snapshot path: 524.
- Missing pre-entry path: 885.
- Analyzed cohort: -$204.26 net, -$0.39/trade, 54.2% win.
- Median freshness: 58.0h.
- Median pre-entry giveback: 9.5pp.

Coverage caveat: early-history trades often lack a usable pre-entry snapshot
path. Treat the all-history result as a robustness check, not a replacement for
the current-regime read.

## Threshold Sweep Results

### TTL-only

Current regime:

| Rule | Rejected n | Rejected net | Kept net | Avoided P&L |
|---|---:|---:|---:|---:|
| freshness > 24h | 247 | -$575.61 | +$1,066.29 | +$575.61 |
| freshness > 48h | 200 | -$728.34 | +$1,219.01 | +$728.34 |
| freshness > 72h | 168 | -$560.84 | +$1,051.51 | +$560.84 |

All history:

| Rule | Rejected n | Rejected net | Kept net | Avoided P&L |
|---|---:|---:|---:|---:|
| freshness > 24h | 373 | -$896.23 | +$691.97 | +$896.23 |
| freshness > 48h | 294 | -$1,330.85 | +$1,126.59 | +$1,330.85 |
| freshness > 72h | 230 | -$1,372.09 | +$1,167.83 | +$1,372.09 |

Read: TTL is directionally useful, but too blunt. It rejects many trades, some
winners, and should probably be used as a downgrade/watch rule rather than a
hard suppressor until actionability data confirms the current gate separation.

### Pre-entry giveback ratio

Best current-regime candidate:

| Rule | Rejected n | Rejected net | Rejected avg | Kept net | Avoided P&L |
|---|---:|---:|---:|---:|---:|
| pre-entry peak >= 40% and giveback >= 50% | 52 | -$962.41 | -$18.51 | +$1,453.09 | +$962.41 |

All-history robustness:

| Rule | Rejected n | Rejected net | Rejected avg | Kept net | Avoided P&L |
|---|---:|---:|---:|---:|---:|
| pre-entry peak >= 40% and giveback >= 50% | 66 | -$1,033.55 | -$15.66 | +$829.29 | +$1,033.55 |

Secondary candidates:

| Rule | Current avoided P&L | All-history avoided P&L | Note |
|---|---:|---:|---|
| pre-entry peak >= 40% and giveback >= 30% | +$757.40 | +$857.01 | Wider, less precise. |
| pre-entry peak >= 75% and giveback >= 50% | +$443.29 | +$467.41 | More precise but smaller n. |
| pre-entry giveback >= 30pp | +$587.75 | +$821.30 | Simple absolute threshold. |

Read: `pre-entry peak >= 40%` plus `giveback >= 50%` is the strongest
candidate. It directly matches the failure mode: system saw a meaningful move,
the token gave back at least half of that move before entry, and the rejected
cohort is negative in both current-regime and all-history views.

### Combined TTL + giveback

Current-regime candidate:

| Rule | Rejected n | Rejected net | Rejected avg | Kept net | Avoided P&L |
|---|---:|---:|---:|---:|---:|
| fresh > 12h + pre-entry peak >= 20% + giveback >= 50% | 65 | -$430.71 | -$6.63 | +$921.38 | +$430.71 |

All-history candidate:

| Rule | Rejected n | Rejected net | Rejected avg | Kept net | Avoided P&L |
|---|---:|---:|---:|---:|---:|
| fresh > 12h + pre-entry peak >= 20% + giveback >= 50% | 93 | -$501.27 | -$5.39 | +$297.00 | +$501.27 |

Read: adding TTL reduces the avoided P&L versus the cleaner `peak >= 40%` /
`giveback >= 50%` rule. Freshness is still useful for triage and UI, but the
giveback ratio is the sharper stale-entry feature.

## Signal Notes

Current-regime signal-level stale-entry cohort:

| Signal | n | Net | Avg | Win | Median fresh h | Median pre-entry giveback pp | Median ratio |
|---|---:|---:|---:|---:|---:|---:|---:|
| losers_contrarian | 118 | -$550.35 | -$4.66 | 58.5% | 84.8 | 33.6 | 0.65 |
| gainers_early | 167 | +$93.90 | +$0.56 | 61.1% | 66.1 | 0.9 | 0.00 |
| volume_spike | 29 | +$516.66 | +$17.82 | 62.1% | 0.0 | 0.0 | 0.00 |
| chain_completed | 9 | +$291.76 | +$32.42 | 77.8% | 154.0 | 31.7 | 0.67 |

Interpretation:

- `losers_contrarian` carries the clearest stale-entry/giveback weakness.
- `volume_spike` does not show the stale-entry pattern; its median freshness and
  pre-entry giveback are both zero in this audit.
- `chain_completed` is profitable despite stale-looking inputs. Do not blindly
  suppress it with a TTL-only rule; if a V2 gate uses the giveback feature,
  preserve signal-specific exceptions until n grows.

## Proposed V2 Gate Candidates

These are design candidates only. Do not implement until the 24h actionability
runbook says whether v1 `actionable=true` separates quality.

1. Add a pre-entry freshness/giveback telemetry computation to the actionability
   evaluator, but keep it non-suppressing at first.
2. Stamp these fields at trade-open time:
   - `first_seen_snapshot_at`
   - `first_seen_price`
   - `pre_entry_peak_price`
   - `pre_entry_peak_at`
   - `freshness_minutes`
   - `pre_entry_peak_gain_pct`
   - `pre_entry_giveback_pp`
   - `pre_entry_giveback_ratio`
3. Candidate V2 reason:
   - `v2_stale_after_pre_entry_peak_giveback`
   - condition: `pre_entry_peak_gain_pct >= 40` and `pre_entry_giveback_ratio >= 0.50`
4. Candidate softer watch reason:
   - `v2_stale_ttl_48h`
   - condition: `freshness_minutes > 2880`
   - use as dashboard/watch downgrade first, not hard suppression.
5. Signal-specific caution:
   - apply the giveback reason as strongest evidence against `losers_contrarian`.
   - require more post-#181 stamped evidence before applying it to `chain_completed`.
   - do not penalize `volume_spike` from this audit.

## Decision Recommendation

After the 24h actionability validation:

- If `actionable=true` clearly outperforms exploratory, design controlled
  capital weighting/suppression with `pre_entry_peak >= 40%` and
  `giveback >= 50%` as a V2 stale-entry feature.
- If exploratory has many winners, keep the classifier non-suppressing and add
  these stale-entry fields to discovery-vs-entry attribution first.

No runtime change should be made from this audit alone.
