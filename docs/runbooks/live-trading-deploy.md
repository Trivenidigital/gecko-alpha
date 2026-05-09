# Live Trading Deploy Runbook

**Status:** spec-as-of-2026-05-09 | **Audience:** operator (manual VPS configuration)

This runbook covers the operator-side prerequisites for flipping
`LIVE_MODE='live'` after M1.5a deploys.

## 1. systemd unit hardening (REQUIRED before LIVE_MODE flip)

Per design-stage R2-C1 finding: smoke-check failure under default systemd
config = sub-second restart loop hitting Binance auth at 50+ req/s →
IP-ban risk within minutes. Required `[Service]` block fields:

```ini
[Service]
Restart=on-failure
RestartSec=30s
StartLimitBurst=3
StartLimitIntervalSec=300s
```

**Effect:** on failure, wait 30s before restart. Allow at most 3 restart
attempts in 300s (5 min). Beyond that, systemd marks the service `failed`
and stops trying — operator must manually intervene with
`systemctl reset-failed gecko-pipeline && systemctl start gecko-pipeline`
after fixing root cause.

### Apply via systemctl edit

```bash
sudo systemctl edit gecko-pipeline.service
# In the [Service] block, paste the 4 fields above.
sudo systemctl daemon-reload
sudo systemctl restart gecko-pipeline
```

### Verify

```bash
ssh root@89.167.116.187 \
  'systemctl show gecko-pipeline.service | grep -E "Restart=|RestartSec=|StartLimitBurst=|StartLimitIntervalUSec="'
```

Expected output:
```
Restart=on-failure
RestartSec=30000000
StartLimitBurst=3
StartLimitIntervalUSec=5min
```

## 2. Out-of-band failure notification (REQUIRED — R2-I4)

`gecko-pipeline.service` failure beyond `StartLimitBurst=3` leaves operator
without out-of-band signal (parent unit's Telegram path is dead). Add
`OnFailure=` directive pointing to a one-shot Telegram notify unit.

### `gecko-pipeline.service` addition

```ini
[Unit]
OnFailure=gecko-pipeline-failure-notify.service
```

### `/etc/systemd/system/gecko-pipeline-failure-notify.service` (NEW)

```ini
[Unit]
Description=Telegram notify on gecko-pipeline failure (M1.5a R2-I4)

[Service]
Type=oneshot
EnvironmentFile=/root/gecko-alpha/.env
ExecStart=/bin/bash -c '\
  if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then \
    curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=🚨 gecko-pipeline.service FAILED — exhausted StartLimitBurst. Operator action required: investigate root cause, then systemctl reset-failed gecko-pipeline && systemctl start gecko-pipeline." \
      --max-time 10 || true; \
  fi'
```

Reload + verify:

```bash
sudo systemctl daemon-reload
systemctl list-unit-files | grep gecko-pipeline-failure-notify
# Expected: gecko-pipeline-failure-notify.service  static
```

## 3. Binance API key prerequisites

Before flipping `LIVE_MODE='live'`, verify all of:

- [ ] **API key has TRADE permission** (Binance API console → Edit
      Restrictions → Enable Spot & Margin Trading)
- [ ] **VPS IP whitelisted** (Binance API console → Edit Restrictions
      → Restrict access to trusted IPs only → add 89.167.116.187)
- [ ] **Account funded with USDT** for the LIVE_TRADE_AMOUNT_USD cap
      (start small: $100 testnet → $50-100 production)
- [ ] **NTP sync on VPS** (Binance recvWindow=10000ms requires clock
      skew < 10s; `timedatectl` should show `NTP service: active`).
      M1.5a uses recvWindow=10000 for jitter tolerance (V1-M3 fold).
- [ ] **First-24h cap recommendation** (PR #86 V3-M3 fold): set
      `LIVE_TRADE_AMOUNT_USD=10` for the first 24h after enabling live;
      after observing 1-3 successful close events with valid
      fill_slippage_bps, raise to the operating cap (default 100).

### Pre-flip whitelist verification (R2-I1, PR #86 V3-I1 fix)

**Important**: `/api/v3/ping` is UNSIGNED and does NOT enforce IP
whitelist — it returns `{}` from any caller. The whitelist is only
enforced on signed requests. To actually verify the whitelist, hit a
signed endpoint (`/api/v3/account`).

The smoke check at boot already does this; for pre-flip verification
the operator can run it manually:

```bash
# This requires HMAC signing — run via the gecko-alpha venv from VPS:
ssh root@89.167.116.187 \
  'cd /root/gecko-alpha && uv run python -c "
import asyncio
from scout.config import Settings
from scout.live.binance_adapter import BinanceSpotAdapter
async def main():
    s = Settings()
    a = BinanceSpotAdapter(s)
    try:
        r = await a._signed_get(\"/api/v3/account\", params={})
        print(\"OK; permissions:\", r.get(\"permissions\"))
    finally:
        await a.close()
asyncio.run(main())
"'
```

Expected output: `OK; permissions: ['SPOT', ...]`. If this fails with
`-2015`, either the API key is wrong, IP whitelist is broken, or key
lacks SPOT permission. Fix BEFORE proceeding.

## 4. .env activation checklist (REQUIRED checklist — R2-I2)

For LIVE_MODE='live' to boot successfully, ALL of these must be set:

- [ ] `LIVE_TRADING_ENABLED=True` (Layer 1 master kill)
- [ ] `LIVE_MODE=live` (Layer 2)
- [ ] `LIVE_USE_REAL_SIGNED_REQUESTS=True` (M1.5a — REQUIRED for
      runtime bodies; default False is the emergency-revert posture)
- [ ] `LIVE_USE_ROUTING_LAYER=True` (M1.5b — REQUIRED for engine
      `_dispatch_live` to fire; default False preserves M1.5a behavior.
      Engine `__init__` raises RuntimeError if this is True without
      `LIVE_USE_REAL_SIGNED_REQUESTS=True` — silent-no-op misconfig
      prevention per design §2.2)
- [ ] `BINANCE_API_KEY=...` (TRADE-scoped key, IP whitelisted)
- [ ] `BINANCE_API_SECRET=...`
- [ ] At least one signal has `live_eligible=1` in signal_params:
      `sqlite3 /root/gecko-alpha/scout.db "UPDATE signal_params SET live_eligible=1 WHERE signal_type='first_signal';"`
- [ ] systemd unit hardened (§1 above)
- [ ] OnFailure notify unit installed (§2 above)

### M1.5b first-dispatch verification (NEW — design §3 R2-I1 fold)

Before flipping `LIVE_USE_ROUTING_LAYER=True`:

```bash
# Confirm first-time activation — venue_health table empty means routing
# falls back to score 0.5 default for the first dispatch (M1.5c recurring
# probe is the structural fix; M1.5b operator activates with eyes-open).
ssh root@89.167.116.187 \
  'sqlite3 /root/gecko-alpha/scout.db "SELECT * FROM venue_health;"' \
  > .venue_health.txt
cat .venue_health.txt  # expected: empty
```

After flipping (within 1 hour at observed ~1.8 signals/hr prod rate, the
first dispatch should fire):

```bash
# Did _dispatch_live fire?
ssh root@89.167.116.187 \
  'journalctl -u gecko-pipeline --since "1 hour ago" | grep live_dispatch_entered' \
  > .dispatch_entered.txt

# Was a terminal status reached?
ssh root@89.167.116.187 \
  'journalctl -u gecko-pipeline --since "1 hour ago" | grep live_dispatch_terminal' \
  > .dispatch_terminal.txt

# Was the counter incremented?
ssh root@89.167.116.187 \
  'sqlite3 /root/gecko-alpha/scout.db "SELECT * FROM signal_venue_correction_count;"' \
  > .counter.txt
```

**Walkaway blast-radius bound** (operator absence):
- exposure ≤ `LIVE_TRADE_AMOUNT_USD × hourly_signal_rate × max_open_per_token × walkaway_hours`
- defaults: `$10 × 2 × 1 × N hours` → 8-hour walkaway = ≤ $160 max exposure

**Counter-reset UX cost** (design §2.5 ack): a single `reset_on_correction`
zeros the entire `consecutive_no_correction` for the (signal_type, venue)
pair. Worked example: 30 successful fills → counter=30 → operator unwinds
trade #31 → counter=0 → all 30 prior good fills lose their auto-clear-
approval progress. M1.5c may add `total_fills_lifetime` for dashboard
telemetry that survives resets.

**Anomaly response:** `LIVE_USE_ROUTING_LAYER=False` in `.env` →
`systemctl restart gecko-pipeline` (~2 second window).

**M1.5a smoke pass ≠ live ready** (R2-I1 historical): boot succeeds =
smoke passed. With M1.5b shipped, the engine routing dispatch + correction
counter writers ARE wired; approval gateway runtime hook + recurring
health probe + reconciliation worker are M1.5c.

## 5. Reversibility (R2-I4 emergency revert)

### Fast revert — `.env` flip (no git)

For M1.5b multi-venue routing dispatch:
```bash
# In .env on VPS:
LIVE_USE_ROUTING_LAYER=False
# systemctl restart gecko-pipeline
# Result: engine bypasses _dispatch_live; M1.5a single-venue path
# resumes. NEW signals are not dispatched via routing layer.
```

For M1.5a signed-request runtime bodies:
```bash
# In .env on VPS:
LIVE_USE_REAL_SIGNED_REQUESTS=False
# systemctl restart gecko-pipeline
# Result: 3 ABC runtime bodies fall back to NotImplementedError;
# Gate 10 returns 'live_signed_disabled' reject_reason.
```

**M1.5b in-flight caveat** (design §5): if engine restart happens
between `place_order_request` and `await_fill_confirmation`, the order
is live on Binance with no engine watcher. The `live_trades` row stays
`status='open'`. Cleanup per §6.

### Slower revert — git revert

**Pre-revert checklist (R2 design fold):**
1. **Set `LIVE_MODE='paper'` BEFORE git revert** — otherwise restored
   NotImplementedError will crash next cycle on Gate 10
2. Verify systemctl status post-restart
3. Watch journal for 60s

```bash
# Local:
git revert <squash-merge-of-PR>
git push
# VPS:
ssh root@89.167.116.187 \
  'cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} +; systemctl restart gecko-pipeline'
```

## 6. Orphaned trade reconciliation (R2-I5 manual runbook)

`live_trades` rows where `place_order_request` succeeded at Binance but
`await_fill_confirmation` timed out leave:
- `entry_order_id IS NOT NULL`
- `status='open'`
- `fill_slippage_bps IS NULL`

M1.5a does NOT auto-reconcile (M1.5b's reconciler does). M1.5a operator
runbook query:

```sql
SELECT id, paper_trade_id, symbol, entry_order_id, created_at
FROM live_trades
WHERE status='open'
  AND fill_slippage_bps IS NULL
  AND entry_order_id IS NOT NULL
  AND created_at < datetime('now', '-10 minutes');
```

Manual remediation per row: query Binance
`GET /api/v3/order?origClientOrderId=...` (signed); read terminal status;
write outcome to live_trades manually.

## 7. M1.5b → M1.5c deferred items

Items closed by M1.5b:
- ✅ Engine wiring of `RoutingLayer.get_candidates` (V1-C1 routing-half)
- ✅ `signal_venue_correction_count.consecutive_no_correction` writer
  (V1-C2 closure — increment on terminal=filled)

Items deferred to M1.5c:
- Engine call to `should_require_approval` before adapter dispatch
  (V1-C1 approval-half)
- Recurring health probe (M1.5a + M1.5b boot smoke is point-in-time;
  closes design §3 R2-I1 venue_health gap structurally)
- Reconciliation worker for orphaned `live_trades` rows (in-flight
  reversibility caveat per §5)
- Automatic `reset_on_correction` triggers (operator-correction window
  detection)
- `total_fills_lifetime` column for dashboard telemetry that survives
  counter resets (design §2.5 UX cost mitigation)
- V2 deferred minors: ServiceRunner cancel-log, view CAST symmetry,
  override-NULL filter, venue_health staleness gate
- BL-055 retirement evaluation gate (3 pre-registered conditions in
  `tasks/design_live_trading_hybrid.md` v2.1)

Do NOT flip `live_eligible=1` for any signal under live mode until
M1.5b ships and the BL-055 retirement gate passes.
