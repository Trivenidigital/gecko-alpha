# BL-075 Phase B — Slow-burn watcher operator runbook

Captures the RIV-shape blind spot: multi-day distributed accumulation
(`7d ≥ 50%` AND `abs(1h) ≤ 5%`) with mcap=0 tolerance.

Research-only — no paper trade dispatch, no scoring integration. Promotes to
signal type only after D+14 soak passes 3 acceptance gates (volume / quality /
separability).

## Live observability

The detector emits a `slow_burn_tokens` log event each cycle a token fires.
Heartbeat output (every 5min) includes `slow_burn_detected_total` — the
process-cumulative count. If this counter stays at 0 for ≥1h after a deploy
that included `SLOW_BURN_ENABLED=True`, the env var likely didn't reach the
running process — check `.env` and restart the systemd unit.

## D+3 sanity check

```bash
ssh srilu-vps 'sqlite3 /root/gecko-alpha/scout.db "
  SELECT COUNT(*), COUNT(DISTINCT coin_id) FROM slow_burn_candidates;
"'
```

Expected: ≥5 unique coins by D+3 (else thresholds may be too tight; investigate).

## D+7 mid-soak verification

```bash
ssh srilu-vps 'curl -s http://localhost:8000/api/slow_burn/stats | python3 -m json.tool'
```

Verifies the dashboard endpoint is wired AND surfaces:
- `unique_coins_14d` — running toward the volume gate (target ≥35 by D+14).
- `mcap_unknown_14d / mcap_known_14d` split — informs whether the Phase A
  blind-spot cohort is dominating the detection set.
- `momentum_7d_overlap_pct` — D+14 separability gate metric (target <70%).

## D+14 acceptance gates

Three gates per BL-075 Phase B plan (R1 reviewer fix). All three must pass
to promote to signal type.

### Gate 1 — Volume (≥35 unique coins)

```sql
SELECT COUNT(DISTINCT coin_id) FROM slow_burn_candidates
WHERE datetime(detected_at) >= datetime('now', '-14 days');
```

If <35: thresholds too tight. Widen `SLOW_BURN_MIN_7D_CHANGE` from 50 → 40
and resoak 7 more days.

### Gate 2 — Quality (≥3 of first 35 hit ≥2x in 30d post-detection)

Manual operator check — query the first 35 detections (in chronological
order) with their detection-time `current_price`, then re-query CG today
and compute % gain. Threshold: ≥3 of those 35 must show ≥2x.

```sql
SELECT coin_id, symbol, detected_at, current_price, market_cap
FROM slow_burn_candidates
ORDER BY detected_at ASC
LIMIT 35;
```

For each row: `https://api.coingecko.com/api/v3/coins/<coin_id>` → check
`market_data.current_price.usd` → compute `(now_price / detected_price) - 1`.
Manual eval; no automation in v1.

If <3 of 35: the signal is no better than random — close BL-075 as
won't-fix-on-this-axis.

### Gate 3 — Separability (<70% momentum_7d overlap)

```sql
SELECT ROUND(100.0 * SUM(also_in_momentum_7d) / COUNT(*), 1) AS overlap_pct
FROM slow_burn_candidates
WHERE datetime(detected_at) >= datetime('now', '-14 days');
```

If ≥70%: slow-burn is just a softer-threshold momentum_7d — not a distinct
signal worth keeping separate. Either drop slow-burn OR widen the 1h gate
to be more restrictive. Operator decision.

## Promotion to signal type (post-D+14)

If all three gates pass, BL-075 Phase B+1 wires `slow_burn` as a paper-
trade signal. Out of this PR's scope; gated on the soak data.

## Revert

```bash
ssh srilu-vps 'sed -i "s/^SLOW_BURN_ENABLED=.*/SLOW_BURN_ENABLED=False/" /root/gecko-alpha/.env'
ssh srilu-vps 'systemctl restart gecko-pipeline'
```

Migration is forward-only; the `slow_burn_candidates` table stays untouched
(may be dropped via separate migration if BL-075 closes won't-fix at D+14).

## Known gaps (documented v1 limitations)

- **Mcap-revealed dedup (R2 plan-stage finding):** if a coin is detected
  with `mcap=NULL` on day 1 and later with `mcap=$10M` on day 4 (within
  `SLOW_BURN_DEDUP_DAYS=7`), the second detection is suppressed. The
  "mcap upgraded" signal is lost. Acceptable for research v1; revisit if
  D+14 quality gate misses tokens that were null-mcap at first detection.

- **Concurrent-pipeline dedup race (R6 PR-stage finding):** the dedup
  guard is a pre-INSERT SELECT (no UNIQUE constraint, per R2 plan
  finding). Two pipelines polling concurrently (operator restart +
  systemd auto-restart on transient SIGTERM) could both pass the dedup
  check and INSERT duplicates. Window is microseconds; not protected
  in v1. If post-soak data shows duplicate rows in `slow_burn_candidates`,
  add `UNIQUE(coin_id, date(detected_at))` via a follow-up migration.
