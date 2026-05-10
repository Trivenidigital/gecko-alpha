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

### Gate 2 — Quality (≥3 of first 35 hit ≥2x within outcome window)

**Methodology pre-registered 2026-05-10 (post-merge user feedback):** lock the
assessment process BEFORE D+14 to prevent drift to "whatever feels right when
staring at the 35 detections."

| Field | Value |
|---|---|
| Sample selection | All 35 detections if exactly 35; first 35 by detection time if more; flag-and-extend soak if fewer than 35 by D+14 |
| Outcome window | **48 hours** from detection-time epoch |
| 2x definition | Price at any point within outcome window ≥ 2× detection-time price (any tick, NOT close-to-close) |
| Reviewer | Operator-only, no LLM-assisted classification |
| Acceptance threshold | **≥3 of 35 (8.6%) hit 2x within outcome window** |

Numbers tunable, structure locked. Same discipline pattern as the operator-
removal criteria pre-registration on the live-trading thread.

#### Step-by-step procedure

1. **Pull the sample** — query rows in chronological order, capturing detection-time
   `current_price` as the baseline:
   ```sql
   SELECT coin_id, symbol, detected_at, current_price, market_cap
   FROM slow_burn_candidates
   ORDER BY detected_at ASC
   LIMIT 35;
   ```
2. **For each coin, fetch the 48h price range post-detection** via CG OHLC:
   `https://api.coingecko.com/api/v3/coins/<coin_id>/ohlc?vs_currency=usd&days=2`
   — returns 30-min candles. Find max(`high`) within (detection_at, detection_at + 48h).
3. **Compute hit ratio:** `max_price_48h / detected_current_price`. Hit if ≥ 2.0.
4. **Tally:** count coins where hit==True. Accept if count ≥ 3.

#### What "high" means

The 2x test is "any tick within window" — using the OHLC `high` field, NOT
the closing price. This catches transient pumps that retrace within the
window. The slow-burn signal's value proposition is "catches early; user
exits at peak via existing trail logic" — the 2x-tick semantic matches that
exit assumption.

#### If <3 of 35 hit 2x

Signal is no better than random for slow-burn detection. Close BL-075 as
won't-fix-on-this-axis. Document the cohort that survived 14d shadow
storage but failed quality gate as anti-evidence for the proposal — useful
for blocking future "let's revisit this" loops.

#### If sample size < 35 by D+14

Flag-and-extend. Continue soak 7 more days, re-evaluate. Do NOT relax the
35-coin floor (statistical power requirement per R1's plan-stage finding).

#### Boundary handling (pre-registered before D+14)

The D+14 evaluation will produce one of three outcomes — pass cleanly, fail
cleanly, or land at the boundary. Pre-register the boundary case here, not
at evaluation time, so post-hoc threshold relaxation isn't an option.

**Boundary cases:**
- 2 of 35 hit 2x (one below threshold).
- 3 of 35 hit 2x BUT one is borderline: `high / detected_price` in `[2.000, 2.05]`.
- 35-coin sample exists AND quality-gate result is within ±1 hit of threshold.

**Boundary action:** extend soak by 14 days. Recompute at D+28 with full
~70-detection sample (assuming current ~21-detections-per-cycle rate holds).

**What NOT to do at boundary:**
- Do NOT change the 2x threshold to fit the result (relaxing to 1.8x post-hoc
  is exactly the drift this pre-registration prevents).
- Do NOT change the 3-of-35 acceptance threshold to fit the result.
- Do NOT include detections post-D+14 in the original 35-coin sample to "rescue"
  the count (extension means new sample, not extended sample).

**If D+28 still at boundary:** escalate as architectural finding, not pass/fail
decision. The signal may be genuinely on a knife-edge between "useful" and
"random" — worth deeper investigation (sub-cohort analysis: mcap-known vs
mcap-unknown rates; chain split; time-of-day clustering) before final verdict.

**Locking rationale:** without this pre-registration, a D+14 of "2 of 35"
becomes grounds to argue "maybe 2 of 35 is fine, the threshold was conservative."
Same discipline pattern as operator-removal pre-registration on the
live-trading thread — locks decision criteria BEFORE seeing the data.

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
